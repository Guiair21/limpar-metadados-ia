#!/usr/bin/env python3
"""Analisa e limpa metadados (C2PA, XMP, EXIF, IPTC) de fotos e vídeos.

Uso:
  python limpar_metadados.py analisar ARQUIVO [ARQUIVO ...]
  python limpar_metadados.py limpar ARQUIVO [ARQUIVO ...] [--saida PASTA]

- JPEG e PNG: remove os blocos de metadados SEM recodificar (pixels idênticos).
  Se o JPEG tem rotação EXIF, gira os pixels e recodifica em qualidade 97.
- MP4/MOV: remux com ffmpeg (-c copy), sem recodificar; descarta tags, capítulos
  e caixas 'uuid' (onde ficam o C2PA e o XMP).
- Nunca altera o original. Grava <nome>_limpo.<ext> na pasta de saída.
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

# Marcas que indicam IA ou credencial de conteúdo dentro do arquivo.
MARCAS = [
    (b"c2pa", "manifesto C2PA (Credenciais de Conteúdo)"),
    (b"jumb", "contêiner JUMBF (usado pelo C2PA)"),
    (b"trainedAlgorithmicMedia", "IPTC: mídia gerada por IA"),
    (b"compositeWithTrainedAlgorithmicMedia", "IPTC: composição com mídia gerada por IA"),
    (b"algorithmicMedia", "IPTC: mídia algorítmica"),
    (b"DigitalSourceType", "campo IPTC DigitalSourceType"),
    (b"Adobe Firefly", "Adobe Firefly"),
    (b"Generative Fill", "Preenchimento Generativo (Adobe)"),
    (b"GenerativeFill", "Preenchimento Generativo (Adobe)"),
    (b"Midjourney", "Midjourney"),
    (b"DALL-E", "DALL-E"),
    (b"DALL\xc2\xb7E", "DALL-E"),
    (b"OpenAI", "OpenAI"),
    (b"Google AI", "Google AI"),
    (b"Made with Google AI", "Google AI"),
    (b"SynthID", "SynthID (texto/metadado)"),
    (b"Stable Diffusion", "Stable Diffusion"),
    (b"Imagen", "Google Imagen"),
    (b"Veo", "Google Veo"),
    (b"Gemini", "Google Gemini"),
    (b"CapCut", "CapCut (tag de encoder)"),
]
# Marcas curtas demais geram falso positivo se vierem soltas dentro de pixels;
# só contam se aparecerem cercadas por não-alfanuméricos.
MARCAS_CURTAS = {b"Veo", b"c2pa", b"jumb", b"OpenAI", b"Imagen", b"Gemini", b"CapCut"}

C2PA_UUID = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")
XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")


def achar_marcas(dados: bytes, tipo: str = None, saida: bool = False):
    if tipo:
        dados = (regioes_do_arquivo_limpo if saida else regioes_de_metadado)(tipo, dados)
    achados = {}
    for marca, rotulo in MARCAS:
        if marca in MARCAS_CURTAS:
            padrao = re.compile(rb"(?<![A-Za-z])" + re.escape(marca) + rb"(?![a-z])")
            if padrao.search(dados):
                achados[rotulo] = achados.get(rotulo, 0) + 1
        elif marca in dados:
            achados[rotulo] = achados.get(rotulo, 0) + 1
    if C2PA_UUID in dados:
        achados["caixa uuid do C2PA (vídeo)"] = 1
    if XMP_UUID in dados:
        achados["caixa uuid de XMP (vídeo)"] = 1
    return sorted(achados)


def regioes_do_arquivo_limpo(tipo: str, dados: bytes) -> bytes:
    """Para conferir a SAÍDA: todo segmento/bloco que não é dado de imagem."""
    out = bytearray()
    if tipo == "jpeg":
        i, n = 2, len(dados)
        while i + 4 <= n and dados[i] == 0xFF:
            m = dados[i + 1]
            if m == 0xD9:
                out += dados[i + 2:]
                break
            if m in (0x01, 0xD8) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            tam = struct.unpack(">H", dados[i + 2:i + 4])[0]
            out += dados[i:i + 2 + tam]
            i += 2 + tam
            if m == 0xDA:
                while i + 1 < n and not (dados[i] == 0xFF and dados[i + 1] not in (0x00, 0xFF) and not 0xD0 <= dados[i + 1] <= 0xD7):
                    i += 1
        return bytes(out)
    if tipo == "png":
        i = 8
        while i + 8 <= len(dados):
            tam = struct.unpack(">I", dados[i:i + 4])[0]
            t = dados[i + 4:i + 8]
            if t not in (b"IDAT", b"fdAT"):
                out += dados[i:i + 12 + tam]
            i += 12 + tam
        return bytes(out)
    return regioes_de_metadado(tipo, dados)


def regioes_de_metadado(tipo: str, dados: bytes) -> bytes:
    """Devolve só os bytes que podem carregar metadado (sem pixels nem amostras)."""
    out = bytearray()
    if tipo == "jpeg":
        try:
            out += _ler_jpeg(dados)[1]
        except ValueError:
            out += dados  # arquivo estranho: varre tudo, melhor um alarme a mais
    elif tipo == "png":
        i = 8
        while i + 8 <= len(dados):
            tam = struct.unpack(">I", dados[i:i + 4])[0]
            tipo_chunk = dados[i + 4:i + 8]
            fim = i + 12 + tam
            if not _chunk_png_ok(tipo_chunk, tam):
                out += dados[i:fim]
            i = fim
            if tipo_chunk == b"IEND":
                out += dados[i:]  # sobra depois do IEND
                break
    elif tipo == "mp4":
        i = 0
        while i + 8 <= len(dados):
            tam = struct.unpack(">I", dados[i:i + 4])[0]
            nome = dados[i + 4:i + 8]
            cab = 8
            if tam == 1:
                tam = struct.unpack(">Q", dados[i + 8:i + 16])[0]
                cab = 16
            elif tam == 0:
                tam = len(dados) - i
            if tam < cab:
                break
            if nome != b"mdat":
                out += dados[i:i + tam]
            i += tam
    return bytes(out)


def tipo_do_arquivo(caminho: str):
    with open(caminho, "rb") as f:
        cab = f.read(16)
    if cab[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if cab[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if cab[4:8] == b"ftyp" and cab[8:12] in (b"avif", b"avis", b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return None  # imagem HEIF/AVIF: fora do escopo (converta para JPG antes)
    if cab[4:8] == b"ftyp" or cab[4:8] in (b"moov", b"free", b"mdat", b"wide"):
        return "mp4"
    return None


# ---------- JPEG ----------
def jpeg_orientacao(dados: bytes) -> int:
    """Lê a orientação EXIF (1 = normal). Retorna 1 se não achar."""
    i = 2
    while i + 4 <= len(dados) and dados[i] == 0xFF:
        marcador = dados[i + 1]
        if marcador == 0xDA:
            break
        tam = struct.unpack(">H", dados[i + 2:i + 4])[0]
        if marcador == 0xE1 and dados[i + 4:i + 10] == b"Exif\x00\x00":
            t = dados[i + 10:i + 2 + tam]
            if len(t) < 8:
                return 1
            end = "<" if t[:2] == b"II" else ">"
            off = struct.unpack(end + "I", t[4:8])[0]
            if off + 2 > len(t):
                return 1
            n = struct.unpack(end + "H", t[off:off + 2])[0]
            for k in range(n):
                e = t[off + 2 + 12 * k: off + 14 + 12 * k]
                if len(e) < 12:
                    break
                if struct.unpack(end + "H", e[:2])[0] == 0x0112:
                    return struct.unpack(end + "H", e[8:10])[0]
        i += 2 + tam
    return 1


def _ler_jpeg(dados: bytes):
    """Percorre o JPEG pela estrutura. Devolve (mantido, descartado).
    descartado = segmentos APPn/COM removidos + tudo depois do EOI da imagem principal."""
    if dados[:2] != b"\xff\xd8":
        raise ValueError("JPEG inválido (sem SOI)")
    mantido = bytearray(b"\xff\xd8")
    descartado = bytearray()
    n = len(dados)
    i = 2
    while i + 2 <= n:
        if dados[i] != 0xFF:
            raise ValueError("JPEG inválido (marcador esperado)")
        while i + 1 < n and dados[i + 1] == 0xFF:  # bytes de preenchimento
            i += 1
        if i + 2 > n:
            break
        m = dados[i + 1]
        if m == 0xD9:  # EOI: fim da imagem principal; o resto é sobra
            mantido += b"\xff\xd9"
            descartado += dados[i + 2:]
            return bytes(mantido), bytes(descartado)
        if m in (0x01, 0xD8) or 0xD0 <= m <= 0xD7:
            mantido += dados[i:i + 2]
            i += 2
            continue
        if i + 4 > n:
            raise ValueError("JPEG truncado")
        tam = struct.unpack(">H", dados[i + 2:i + 4])[0]
        if tam < 2 or i + 2 + tam > n:
            raise ValueError("JPEG corrompido (segmento com tamanho inválido)")
        bloco = dados[i:i + 2 + tam]
        i += 2 + tam
        if m == 0xDA:  # varredura: copia o bloco e os dados comprimidos até o próximo marcador real
            mantido += bloco
            j = i
            while j + 1 < n:
                if dados[j] == 0xFF and dados[j + 1] != 0x00 and not (0xD0 <= dados[j + 1] <= 0xD7) and dados[j + 1] != 0xFF:
                    break
                j += 1
            else:
                raise ValueError("JPEG truncado (sem fim de imagem)")
            mantido += dados[i:j]
            i = j
            continue
        manter = False
        if m == 0xE0:  # JFIF exato (16 bytes); JFXX e outros APP0 saem
            manter = tam == 16 and bloco[4:9] == b"JFIF\x00"
        elif m == 0xE2 and bloco[4:16].startswith(b"ICC_PROFILE"):
            manter = True  # perfil de cor, não é metadado de autoria
        elif m == 0xEE:  # flag de espaço de cor da Adobe, exata (14 bytes)
            manter = tam == 14 and bloco[4:9] == b"Adobe"
        elif not (0xE0 <= m <= 0xEF or m == 0xFE):
            manter = True  # tabelas e quadros da imagem
        if manter:
            mantido += bloco
        else:
            descartado += bloco
    raise ValueError("JPEG truncado (sem fim de imagem)")


def _tirar_segmentos_jpeg(dados: bytes) -> bytes:
    return _ler_jpeg(dados)[0]


def limpar_jpeg(origem: str, destino: str):
    dados = open(origem, "rb").read()
    if jpeg_orientacao(dados) != 1:
        import io
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(origem))
        icc = img.info.get("icc_profile")
        extra = {"icc_profile": icc} if icc else {}
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=97, subsampling=0, optimize=True, **extra)
        open(destino, "wb").write(_tirar_segmentos_jpeg(buf.getvalue()))  # tira o COM que o Pillow regrava
        return "recodificado (rotação EXIF aplicada, qualidade 97)"
    open(destino, "wb").write(_tirar_segmentos_jpeg(dados))
    return "sem recodificar (pixels idênticos)"


# ---------- PNG ----------
PNG_MANTER = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB",
              b"iCCP", b"sBIT", b"pHYs", b"bKGD", b"hIST", b"acTL", b"fcTL", b"fdAT",
              b"cICP", b"mDCV", b"cLLI"}


PNG_TAMANHO = {b"IHDR": (13,), b"gAMA": (4,), b"cHRM": (32,), b"sRGB": (1,), b"pHYs": (9,),
               b"sBIT": (1, 2, 3, 4), b"bKGD": (1, 2, 6), b"cICP": (4,), b"acTL": (8,), b"fcTL": (26,)}


def _chunk_png_ok(tipo: bytes, tam: int) -> bool:
    return tipo in PNG_MANTER and (tipo not in PNG_TAMANHO or tam in PNG_TAMANHO[tipo])


def limpar_png(origem: str, destino: str):
    dados = open(origem, "rb").read()
    saida = bytearray(dados[:8])
    i = 8
    fechou = False
    while i + 8 <= len(dados):
        tam = struct.unpack(">I", dados[i:i + 4])[0]
        tipo = dados[i + 4:i + 8]
        fim = i + 12 + tam
        if fim > len(dados):
            raise ValueError("PNG truncado")
        if tipo == b"IHDR" and tam != 13:
            raise ValueError("PNG corrompido (IHDR inválido)")
        if _chunk_png_ok(tipo, tam):
            saida += dados[i:fim]
        i = fim
        if tipo == b"IEND":
            fechou = True
            break
    if not fechou:
        raise ValueError("PNG sem IEND (truncado)")
    open(destino, "wb").write(bytes(saida))
    return "sem recodificar (pixels idênticos)"


# ---------- MP4 / MOV ----------
def limpar_mp4(origem: str, destino: str):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg não encontrado no PATH")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", origem,
        "-map", "0:v?", "-map", "0:a?",
        "-c", "copy",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-metadata:s:v", "handler_name=", "-metadata:s:a", "handler_name=",
        "-movflags", "+faststart",
        destino,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "ffmpeg falhou")
    return "remux sem recodificar (vídeo e áudio idênticos)"


def validar_saida(tipo: str, caminho: str):
    """Garante que o arquivo limpo abre de verdade."""
    if tipo in ("jpeg", "png"):
        from PIL import Image
        with Image.open(caminho) as im:
            im.load()
    else:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", caminho, "-f", "null", "-"],
                           capture_output=True, text=True)
        if r.returncode != 0 or r.stderr.strip():
            raise RuntimeError("o vídeo limpo não abre: " + r.stderr.strip()[:200])


LIMPADORES = {"jpeg": limpar_jpeg, "png": limpar_png, "mp4": limpar_mp4}


def analisar(caminho: str):
    tipo = tipo_do_arquivo(caminho)
    if not tipo:
        print(f"[?] {caminho}: formato não suportado (use JPG, PNG, MP4 ou MOV)")
        return None
    dados = open(caminho, "rb").read()
    marcas = achar_marcas(dados, tipo)
    if marcas:
        print(f"[!] {os.path.basename(caminho)} ({tipo}): marcas encontradas")
        for m in marcas:
            print(f"      - {m}")
    else:
        print(f"[ok] {os.path.basename(caminho)} ({tipo}): nenhuma marca de IA nos metadados")
    return marcas


def limpar(caminho: str, pasta_saida: str):
    tipo = tipo_do_arquivo(caminho)
    if not tipo:
        print(f"[?] {caminho}: formato não suportado")
        return False
    base, ext = os.path.splitext(os.path.basename(caminho))
    os.makedirs(pasta_saida, exist_ok=True)
    destino = os.path.join(pasta_saida, f"{base}_limpo{ext}")
    antes = achar_marcas(open(caminho, "rb").read(), tipo)
    try:
        modo = LIMPADORES[tipo](caminho, destino)
        validar_saida(tipo, destino)
    except Exception as e:
        if os.path.exists(destino):
            os.remove(destino)
        print(f"[x] {os.path.basename(caminho)}: {e}")
        return False
    depois = achar_marcas(open(destino, "rb").read(), tipo, saida=True)
    print(f"{os.path.basename(caminho)} -> {destino}")
    print(f"   método: {modo}")
    print(f"   antes : {len(antes)} marca(s) {antes if antes else ''}")
    print(f"   depois: {len(depois)} marca(s) {depois if depois else ''}")
    if depois:
        print("   [x] AINDA há marcas no arquivo limpo; não use este arquivo")
        return False
    print("   [ok] arquivo limpo")
    return True


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("acao", choices=["analisar", "limpar"])
    p.add_argument("arquivos", nargs="+")
    p.add_argument("--saida", help="pasta de saída (padrão: subpasta 'limpos' ao lado do original)")
    a = p.parse_args()
    ok = True
    for arq in a.arquivos:
        if not os.path.isfile(arq):
            print(f"[x] {arq}: arquivo não encontrado")
            ok = False
            continue
        if a.acao == "analisar":
            analisar(arq)
        else:
            pasta = a.saida or os.path.join(os.path.dirname(os.path.abspath(arq)), "limpos")
            try:
                ok = limpar(arq, pasta) and ok
            except Exception as e:
                print(f"[x] {arq}: {e}")
                ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
