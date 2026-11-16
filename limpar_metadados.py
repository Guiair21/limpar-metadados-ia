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
import json
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
# As de 3 e 4 letras aparecem por acaso em dado binário (paleta, tabelas de
# amostras, perfil de cor), então só contam onde cabe texto.
MARCAS_SO_EM_TEXTO = {b"Veo", b"c2pa", b"jumb"}

C2PA_UUID = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")
XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")


def achar_marcas(dados: bytes, tipo: str = None, saida: bool = False):
    """Procura as marcas. Sem `tipo`, varre os bytes como vieram.
    Com `tipo`, separa o arquivo em duas regiões: TEXTO (onde cabe metadado
    escrito) e BINÁRIO (tabelas, paleta, perfil de cor). As marcas de 3 e 4
    letras só contam no TEXTO, porque em dado binário elas aparecem por acaso."""
    achados = {}
    if tipo:
        texto, binario, estrutura = (regioes_do_arquivo_limpo if saida else regioes_de_metadado)(tipo, dados)
        for rotulo in estrutura:
            achados[rotulo] = 1
    else:
        texto, binario = dados, b""
    tudo = texto + b"\x00" + binario
    for marca, rotulo in MARCAS:
        onde = texto if (saida or marca in MARCAS_SO_EM_TEXTO) else tudo
        if marca in MARCAS_CURTAS:
            padrao = re.compile(rb"(?<![A-Za-z])" + re.escape(marca) + rb"(?![a-z])")
            if padrao.search(onde):
                achados[rotulo] = achados.get(rotulo, 0) + 1
        elif marca in onde:
            achados[rotulo] = achados.get(rotulo, 0) + 1
    if C2PA_UUID in tudo:
        achados["caixa uuid do C2PA (vídeo)"] = 1
    if XMP_UUID in tudo:
        achados["caixa uuid de XMP (vídeo)"] = 1
    return sorted(achados)


def _jpeg_permitido(m: int, tam: int, bloco: bytes) -> bool:
    """Os únicos segmentos APPn que a limpeza mantém; COM nunca fica."""
    if m == 0xE0:  # JFIF exato (16 bytes); JFXX e outros APP0 saem
        return tam == 16 and bloco[4:9] == b"JFIF\x00"
    if m == 0xE2:  # perfil de cor, não é metadado de autoria
        return bloco[4:16].startswith(b"ICC_PROFILE")
    if m == 0xEE:  # flag de espaço de cor da Adobe, exata (14 bytes)
        return tam == 14 and bloco[4:9] == b"Adobe"
    return not (0xE0 <= m <= 0xEF or m == 0xFE)  # tabelas e quadros da imagem


def regioes_do_arquivo_limpo(tipo: str, dados: bytes):
    """Para conferir a SAÍDA. Devolve (texto, binario, estrutura).
    estrutura = lista do que não deveria existir num arquivo limpo."""
    texto, binario, estrutura = bytearray(), bytearray(), []
    if tipo == "jpeg":
        i, n = 2, len(dados)
        while i + 4 <= n and dados[i] == 0xFF:
            m = dados[i + 1]
            if m == 0xD9:
                if dados[i + 2:]:
                    estrutura.append("dados depois do fim da imagem (JPG)")
                    texto += dados[i + 2:]
                break
            if m in (0x01, 0xD8) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            tam = struct.unpack(">H", dados[i + 2:i + 4])[0]
            bloco = dados[i:i + 2 + tam]
            if _jpeg_permitido(m, tam, bloco):
                binario += bloco
            else:
                estrutura.append("bloco de metadado que deveria ter saído (JPG)")
                texto += bloco
            i += 2 + tam
            if m == 0xDA:
                while i + 1 < n and not (dados[i] == 0xFF and dados[i + 1] not in (0x00, 0xFF) and not 0xD0 <= dados[i + 1] <= 0xD7):
                    i += 1
        return bytes(texto), bytes(binario), sorted(set(estrutura))
    if tipo == "png":
        i = 8
        while i + 8 <= len(dados):
            tam = struct.unpack(">I", dados[i:i + 4])[0]
            t = dados[i + 4:i + 8]
            fim = i + 12 + tam
            if not _chunk_png_ok(t, tam):
                estrutura.append("bloco de metadado que deveria ter saído (PNG)")
                texto += dados[i:fim]
            elif t not in (b"IDAT", b"fdAT"):  # inclui o perfil de cor, mantido inteiro
                binario += dados[i:fim]
            i = fim
            if t == b"IEND":
                if dados[i:]:
                    estrutura.append("dados depois do fim da imagem (PNG)")
                    texto += dados[i:]
                break
        return bytes(texto), bytes(binario), sorted(set(estrutura))
    return regioes_de_metadado(tipo, dados)


# Caixas do MP4/MOV que só agrupam outras, e caixas que são tabela numérica pura.
_MP4_CONTEINERES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"mvex",
                    b"moof", b"traf", b"mfra", b"dinf"}
_MP4_TABELAS = {b"stts", b"ctts", b"stsc", b"stsz", b"stz2", b"stco", b"co64", b"stss",
                b"stps", b"sdtp", b"sbgp", b"saiz", b"saio", b"senc", b"trun", b"tfra",
                b"sidx", b"elst", b"cslg", b"subs"}


def _mp4_regioes(dados: bytes, i: int, fim: int, texto: bytearray, binario: bytearray):
    while i + 8 <= fim:
        tam = struct.unpack(">I", dados[i:i + 4])[0]
        nome = dados[i + 4:i + 8]
        cab = 8
        if tam == 1:
            if i + 16 > fim:
                break
            tam = struct.unpack(">Q", dados[i + 8:i + 16])[0]
            cab = 16
        elif tam == 0:
            tam = fim - i
        if tam < cab or i + tam > fim:
            texto += dados[i:fim]  # estrutura estranha: varre como texto, melhor um alarme a mais
            return
        if nome == b"mdat":
            pass  # amostras de vídeo e áudio
        elif nome in _MP4_TABELAS:
            binario += dados[i:i + tam]
        elif nome in _MP4_CONTEINERES:
            texto += dados[i:i + cab]
            _mp4_regioes(dados, i + cab, i + tam, texto, binario)
        else:
            texto += dados[i:i + tam]
        i += tam


def regioes_de_metadado(tipo: str, dados: bytes):
    """Para a ENTRADA. Devolve (texto, binario, estrutura): só os bytes que
    podem carregar metadado, sem pixels nem amostras."""
    texto, binario = bytearray(), bytearray()
    if tipo == "jpeg":
        try:
            _, descartado, sobra = _ler_jpeg(dados)
            texto += descartado
            binario += sobra  # miniatura, vídeo de Motion Photo: dado binário
        except ValueError:
            texto += dados  # arquivo estranho: varre tudo, melhor um alarme a mais
    elif tipo == "png":
        i = 8
        while i + 8 <= len(dados):
            tam = struct.unpack(">I", dados[i:i + 4])[0]
            tipo_chunk = dados[i + 4:i + 8]
            fim = i + 12 + tam
            if not _chunk_png_ok(tipo_chunk, tam):
                texto += dados[i:fim]
            i = fim
            if tipo_chunk == b"IEND":
                binario += dados[i:]  # sobra depois do IEND
                break
    elif tipo == "mp4":
        _mp4_regioes(dados, 0, len(dados), texto, binario)
    return bytes(texto), bytes(binario), []


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
    """Percorre o JPEG pela estrutura. Devolve (mantido, descartado, sobra).
    descartado = segmentos APPn/COM removidos; sobra = tudo depois do EOI da imagem principal."""
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
            return bytes(mantido), bytes(descartado), bytes(dados[i + 2:])
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
        manter = _jpeg_permitido(m, tam, bloco)
        if manter:
            mantido += bloco
        else:
            descartado += bloco
    raise ValueError("JPEG truncado (sem fim de imagem)")


def _tirar_segmentos_jpeg(dados: bytes) -> bytes:
    return _ler_jpeg(dados)[0]


def limpar_jpeg(origem: str, destino: str):
    dados = open(origem, "rb").read()
    if jpeg_orientacao(dados) in (2, 3, 4, 5, 6, 7, 8):  # 0, 9 e outros valores inválidos não giram
        import io
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = None  # arquivo do próprio usuário; foto de 200 MP não é ataque
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
        "-map", "0:V?", "-map", "0:a?",  # V maiúsculo: sem capa nem miniatura embutida
        "-c", "copy",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-metadata:s:v", "handler_name=", "-metadata:s:a", "handler_name=",
        "-movflags", "+faststart",
        destino,
    ]
    r = _rodar(cmd)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip() or "ffmpeg falhou")
    return "remux sem recodificar (vídeo e áudio idênticos)"


def _rodar(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _decodifica(caminho: str):
    """Decodifica o vídeo e o áudio inteiros. Devolve (código, mensagens de erro).
    Sem grade fixa de quadros (passthrough): vídeo de taxa variável é válido e
    não pode gerar aviso de tempo fora de ordem."""
    base = ["ffmpeg", "-v", "error", "-i", caminho, "-map", "0:V?", "-map", "0:a?"]
    r = _rodar(base + ["-fps_mode", "passthrough", "-f", "null", "-"])
    if r.returncode != 0 and "fps_mode" in r.stderr:  # ffmpeg antigo (antes do 5.1)
        r = _rodar(base + ["-vsync", "passthrough", "-f", "null", "-"])
    return r.returncode, r.stderr.strip()


def _hash_dos_pacotes(caminho: str):
    """Um hash por fluxo de vídeo e de áudio, sobre os dados gravados, sem
    decodificar. None se este ffmpeg não souber fazer."""
    r = _rodar(["ffmpeg", "-v", "error", "-i", caminho, "-map", "0:V?", "-map", "0:a?",
                "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-"])
    if r.returncode != 0:
        return None
    return [linha.split(",", 1)[1] for linha in r.stdout.splitlines() if "," in linha]


def _numero(texto):
    try:
        return float(texto)
    except (TypeError, ValueError):
        return None


def _fluxos(caminho: str):
    """Por fluxo de vídeo e de áudio: (parâmetros que mudam o jeito de
    decodificar, duração exibida em segundos, quadros por segundo).
    A duração já vem com a lista de edição aplicada. None sem ffprobe."""
    if not shutil.which("ffprobe"):
        return None
    r = _rodar(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height,pix_fmt,has_b_frames,sample_rate,channels,"
                "duration,avg_frame_rate:stream_disposition=attached_pic", "-of", "json", caminho])
    if r.returncode != 0:
        return None
    try:
        fluxos = json.loads(r.stdout).get("streams", [])
    except ValueError:
        return None
    out = []
    for f in fluxos:
        duracao = _numero(f.get("duration"))
        if f.get("codec_type") == "video" and not (f.get("disposition") or {}).get("attached_pic"):
            num, _, den = (f.get("avg_frame_rate") or "0/1").partition("/")
            qps = (_numero(num) or 0) / (_numero(den) or 1)
            out.append((("vídeo", f.get("codec_name"), f.get("width"), f.get("height"),
                         f.get("pix_fmt"), f.get("has_b_frames")), duracao, qps))
        elif f.get("codec_type") == "audio":
            out.append((("áudio", f.get("codec_name"), f.get("sample_rate"), f.get("channels")),
                        duracao, 0))
    return out


def validar_saida(tipo: str, caminho: str, origem: str):
    """Garante que o arquivo limpo abre de verdade e, no vídeo, que o remonte
    não mexeu no conteúdo: mesmos dados e mesmos parâmetros de decodificação."""
    if tipo in ("jpeg", "png"):
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None  # arquivo do próprio usuário; foto de 200 MP não é ataque
        with Image.open(caminho) as im:
            im.load()
        return
    codigo, erros = _decodifica(caminho)
    if codigo != 0:
        raise RuntimeError("o vídeo limpo não decodifica: " + erros[:200])
    if erros and not _decodifica(origem)[1]:
        raise RuntimeError("o vídeo limpo decodifica com erro que o original não tem: " + erros[:200])
    h_origem, h_limpo = _hash_dos_pacotes(origem), _hash_dos_pacotes(caminho)
    if h_origem is not None and h_limpo is not None and h_origem != h_limpo:
        raise RuntimeError("o remonte não preservou os dados de vídeo e de áudio")
    f_origem, f_limpo = _fluxos(origem), _fluxos(caminho)
    if f_origem is None or f_limpo is None:
        return
    if [f[0] for f in f_origem] != [f[0] for f in f_limpo]:
        raise RuntimeError("o remonte mudou um parâmetro de decodificação e o vídeo sairia alterado "
                           f"(original {[f[0] for f in f_origem]}, limpo {[f[0] for f in f_limpo]})")
    for (param, dur_o, qps), (_, dur_l, _) in zip(f_origem, f_limpo):
        if dur_o is None or dur_l is None:
            continue
        # folga: um quadro e meio no vídeo; no áudio, o ajuste de início e fim do AAC
        folga = max(0.05, 1.5 / qps) if qps else 0.12
        if dur_l > dur_o + folga:
            raise RuntimeError(f"a cópia mostraria um trecho que o original esconde ({param[0]}: {dur_o:.3f} s "
                               f"no original, {dur_l:.3f} s na cópia). Acontece em vídeo cortado sem "
                               "recodificar; exporte o vídeo de novo e limpe o arquivo exportado")
        if dur_l < dur_o - folga:
            raise RuntimeError(f"a cópia ficaria mais curta que o original ({param[0]}: {dur_o:.3f} s "
                               f"no original, {dur_l:.3f} s na cópia)")


def _ler_para_conferir(caminho: str, tipo: str) -> bytes:
    """Bytes onde pode haver metadado. No vídeo, pula as amostras (mdat) sem
    carregá-las: um arquivo de vários GB não pode ir inteiro para a memória."""
    if tipo != "mp4":
        with open(caminho, "rb") as f:
            return f.read()
    out = bytearray()
    total = os.path.getsize(caminho)
    with open(caminho, "rb") as f:
        pos = 0
        while pos + 8 <= total:
            f.seek(pos)
            cab = f.read(16)
            tam = struct.unpack(">I", cab[:4])[0]
            nome = cab[4:8]
            n = 8
            if tam == 1:
                if len(cab) < 16:
                    break
                tam = struct.unpack(">Q", cab[8:16])[0]
                n = 16
            elif tam == 0:
                tam = total - pos
            if tam < n or pos + tam > total:
                f.seek(pos)
                out += f.read(64 * 1024 * 1024)  # estrutura estranha: lê um trecho e para
                break
            if nome != b"mdat":
                f.seek(pos)
                out += f.read(tam)
            pos += tam
    return bytes(out)


LIMPADORES = {"jpeg": limpar_jpeg, "png": limpar_png, "mp4": limpar_mp4}


def analisar(caminho: str):
    tipo = tipo_do_arquivo(caminho)
    if not tipo:
        print(f"[?] {caminho}: formato não suportado (use JPG, PNG, MP4 ou MOV)")
        return None
    marcas = achar_marcas(_ler_para_conferir(caminho, tipo), tipo)
    if marcas:
        print(f"[!] {os.path.basename(caminho)} ({tipo}): marcas encontradas")
        for m in marcas:
            print(f"      - {m}")
    else:
        print(f"[ok] {os.path.basename(caminho)} ({tipo}): nenhuma marca de IA nos metadados")
    return marcas


def _chave(caminho: str) -> str:
    """O mesmo arquivo dá a mesma chave, ainda que o caminho venha por junção,
    link ou nome curto do Windows (por isso realpath, e não abspath)."""
    return os.path.normcase(os.path.realpath(caminho))


def _destino_livre(pasta_saida: str, base: str, ext: str, usados: set) -> str:
    """O destino nunca coincide com um arquivo pedido nem com outra cópia desta
    execução: na colisão, o nome ganha _limpo_2, _limpo_3..."""
    destino = os.path.join(pasta_saida, f"{base}_limpo{ext}")
    n = 2
    while _chave(destino) in usados:
        destino = os.path.join(pasta_saida, f"{base}_limpo_{n}{ext}")
        n += 1
    usados.add(_chave(destino))
    return destino


def limpar(caminho: str, pasta_saida: str, usados: set = None):
    tipo = tipo_do_arquivo(caminho)
    if not tipo:
        print(f"[?] {caminho}: formato não suportado (use JPG, PNG, MP4 ou MOV)")
        return False
    base, ext = os.path.splitext(os.path.basename(caminho))
    os.makedirs(pasta_saida, exist_ok=True)
    destino = _destino_livre(pasta_saida, base, ext, usados if usados is not None else set())
    antes = achar_marcas(_ler_para_conferir(caminho, tipo), tipo)
    try:
        modo = LIMPADORES[tipo](caminho, destino)
        validar_saida(tipo, destino, caminho)
    except Exception as e:
        if os.path.exists(destino):
            os.remove(destino)
        print(f"[x] {os.path.basename(caminho)}: {e}")
        return False
    depois = achar_marcas(_ler_para_conferir(destino, tipo), tipo, saida=True)
    print(f"{os.path.basename(caminho)} -> {destino}")
    print(f"   método: {modo}")
    print(f"   antes : {len(antes)} marca(s) {antes if antes else ''}")
    print(f"   depois: {len(depois)} marca(s) {depois if depois else ''}")
    if depois:
        os.remove(destino)
        print("   [x] AINDA há marcas depois da limpeza; a cópia foi descartada")
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
    # destinos já gravados nesta execução; começa com os próprios pedidos,
    # para a cópia limpa de um nunca cair em cima do original de outro
    usados = {_chave(x) for x in a.arquivos}
    nao_limpos = []  # pedidos que ficaram de fora; o motivo já foi impresso acima
    for arq in a.arquivos:
        if not os.path.isfile(arq):
            motivo = "é uma pasta; passe os arquivos dela" if os.path.isdir(arq) else "arquivo não encontrado"
            print(f"[x] {arq}: {motivo}")
            ok = False
            nao_limpos.append(arq)
            continue
        if a.acao == "analisar":
            analisar(arq)
        else:
            pasta = a.saida or os.path.join(os.path.dirname(os.path.abspath(arq)), "limpos")
            try:
                feito = limpar(arq, pasta, usados)
            except Exception as e:
                print(f"[x] {arq}: {e}")
                feito = False
            if not feito:
                nao_limpos.append(arq)
            ok = feito and ok
    if a.acao == "limpar":
        total = len(a.arquivos)
        print(f"Resumo: {total - len(nao_limpos)} de {total} arquivo(s) limpo(s)")
        for arq in nao_limpos:
            print(f"   não limpo: {arq}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
