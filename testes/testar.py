#!/usr/bin/env python3
"""Teste de ponta a ponta da skill limpar-metadados-ia.

Uso (a partir da raiz da pasta da skill):
    python testes/testar.py

Cria arquivos de teste numa pasta temporária, chama limpar_metadados.py pela
linha de comando e confere a saída com leitura própria dos bytes (nada é
importado do script testado, com TRÊS exceções marcadas: os casos (o), (v) e (w) importam o
módulo para chamar achar_marcas, _ler_para_conferir e validar_saida direto). Usa só a
biblioteca padrão, o Pillow e o ffmpeg do PATH. A pasta temporária é apagada no fim.
"""
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
import zlib
from fractions import Fraction
from pathlib import Path

sys.dont_write_bytecode = True
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = Path(os.environ.get("LIMPAR_SCRIPT") or RAIZ / "limpar_metadados.py")  # LIMPAR_SCRIPT: só para testar o próprio teste

try:
    from PIL import Image, ImageChops, ImageStat
except ImportError:
    print("[FALHOU] Pillow não está instalado (pip install pillow)")
    sys.exit(1)

FFMPEG = shutil.which("ffmpeg")

TOTAL = 0
FALHAS = []
PULADOS = []
HASHES = {}  # caminho do original -> sha256 no momento da criação

C2PA_UUID = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")
XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")


# ---------------------------------------------------------------- utilitários
def checar(cond, nome, motivo=""):
    global TOTAL
    TOTAL += 1
    if cond:
        print(f"[ok] {nome}")
    else:
        msg = f"{nome}: {motivo}" if motivo else nome
        print(f"[FALHOU] {msg}")
        FALHAS.append(msg)
    return bool(cond)


def sha(caminho):
    return hashlib.sha256(Path(caminho).read_bytes()).hexdigest()


def gravar_original(caminho, dados):
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(dados)
    HASHES[str(caminho)] = sha(caminho)
    return str(caminho)


def registrar_original_existente(caminho):
    HASHES[str(caminho)] = sha(caminho)


def rodar(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                       capture_output=True, cwd=cwd, env=env)
    saida = r.stdout.decode("utf-8", errors="replace") + r.stderr.decode("utf-8", errors="replace")
    return r.returncode, saida


def ultima_linha_util(texto):
    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    return linhas[-1] if linhas else ""


def faltando_em(dados, cadeias):
    """Cadeias (bytes) que NÃO aparecem em dados."""
    return [c for c in cadeias if c not in dados]


def presentes_em(dados, cadeias):
    """Cadeias (bytes) que aparecem em dados."""
    return [c for c in cadeias if c in dados]


def mostrar(lista):
    return ", ".join(repr(x[:40]) for x in lista)


# ------------------------------------------------------------ imagens de base
def imagem(w=150, h=98, var=0):
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 255 // w + var) % 256, (y * 255 // h) % 256, ((x // 8 + y // 8) * 40 + var) % 256)
    for y in range(0, 20):          # marca assimétrica para enxergar rotação
        for x in range(0, 40):
            px[x, y] = (255, 0, 0)
    return im


def jpeg_puro(im=None, **kw):
    im = im or imagem()
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90, **kw)
    return b.getvalue()


def png_puro(im=None, **kw):
    im = im or imagem()
    b = io.BytesIO()
    im.save(b, "PNG", **kw)
    return b.getvalue()


def pixels(dados):
    with Image.open(io.BytesIO(dados)) as im:
        im.load()
        return im.mode, im.size, im.tobytes(), repr(im.info.get("transparency"))


def perfil_icc():
    try:
        from PIL import ImageCms
        return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    except Exception:
        return b"\x00\x00\x03\x00" + b"PERFIL-ICC-FALSO-DE-TESTE" + b"\x00" * 300


# ---------------------------------------------------------------- EXIF à mão
def montar_exif(orient=1, little=True):
    e = "<" if little else ">"

    def asc(s):
        return s.encode() + b"\0"

    lat = b"".join(struct.pack(e + "II", a, b) for a, b in ((23, 1), (33, 1), (4444, 100)))
    lon = b"".join(struct.pack(e + "II", a, b) for a, b in ((46, 1), (38, 1), (5555, 100)))
    make, model, data = "CAMERA-MARCA-INJETADA", "CAMERA-MODELO-INJETADO", "2031:01:02 03:04:05"

    def construir(entradas, inicio):
        n = len(entradas)
        cab = 2 + 12 * n + 4
        dados, tabela = b"", b""
        for tag, tipo, cnt, val in sorted(entradas):
            if len(val) <= 4:
                v = val.ljust(4, b"\0")
            else:
                v = struct.pack(e + "I", inicio + cab + len(dados))
                dados += val + (b"\0" if len(val) % 2 else b"")
            tabela += struct.pack(e + "HHI", tag, tipo, cnt) + v
        return struct.pack(e + "H", n) + tabela + b"\0\0\0\0" + dados

    def ifd0(ptr):
        return [(0x010F, 2, len(asc(make)), asc(make)), (0x0110, 2, len(asc(model)), asc(model)),
                (0x0112, 3, 1, struct.pack(e + "H", orient)), (0x0132, 2, len(asc(data)), asc(data)),
                (0x8825, 4, 1, struct.pack(e + "I", ptr))]

    gps = [(1, 2, 2, b"S\0"), (2, 5, 3, lat), (3, 2, 2, b"W\0"), (4, 5, 3, lon)]
    tam0 = len(construir(ifd0(0), 8))
    gps_off = 8 + tam0
    tiff = (b"II*\0" if little else b"MM\0*") + struct.pack(e + "I", 8) + construir(ifd0(gps_off), 8) + construir(gps, gps_off)
    blobs = [make.encode(), model.encode(), data.encode(), lat, lon]
    return tiff, blobs


def seg(marcador, carga):
    return b"\xff" + bytes([marcador]) + struct.pack(">H", len(carga) + 2) + carga


def caixa(tipo, carga):
    return struct.pack(">I", 8 + len(carga)) + tipo + carga


XMP_IA = (b"<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?><x:xmpmeta xmlns:x='adobe:ns:meta/'>"
          b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
          b"<rdf:Description xmlns:Iptc4xmpExt='http://iptc.org/std/Iptc4xmpExt/2008-02-29/' rdf:about='XMP-ID-INJETADO'>"
          b"<Iptc4xmpExt:DigitalSourceType>http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
          b"</Iptc4xmpExt:DigitalSourceType></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end='w'?>")

JUMBF = caixa(b"jumb",
              caixa(b"jumd", bytes.fromhex("63327061" "00110010800000aa00389b71") + b"\x03" + b"c2pa\0")
              + caixa(b"json", b'{"claim":"C2PA-MANIFESTO-INJETADO"}'))


def iptc_app13():
    r = (b"\x1c\x02\x05" + struct.pack(">H", 20) + b"IPTC-TITULO-INJETADO"
         + b"\x1c\x02\x19" + struct.pack(">H", 21) + b"IPTC-PALAVRA-INJETADA")
    if len(r) % 2:
        r += b"\0"
    return b"Photoshop 3.0\0" + b"8BIM" + struct.pack(">H", 0x0404) + b"\0\0" + struct.pack(">I", len(r)) + r


CADEIAS_JPG_FIXAS = [
    b"trainedAlgorithmicMedia", b"DigitalSourceType", b"c2pa", b"jumb", b"C2PA-MANIFESTO-INJETADO",
    b"IPTC-TITULO-INJETADO", b"IPTC-PALAVRA-INJETADA", b"Midjourney", b"COMENTARIO-INJETADO",
    b"LIXO-FINAL-INJETADO", b"XMP-ID-INJETADO", b"Exif\0\0", b"http://ns.adobe.com/xap/1.0/",
    b"Photoshop 3.0",
]


def jpeg_com_tudo(im=None, orient=1, little=True, **kw):
    """JPEG com EXIF(GPS, câmera, data), XMP IA, JUMBF c2pa, IPTC, COM e lixo depois do fim."""
    base = jpeg_puro(im, **kw)
    tiff, blobs = montar_exif(orient, little)
    segs = (seg(0xE1, b"Exif\0\0" + tiff)
            + seg(0xE1, b"http://ns.adobe.com/xap/1.0/\0" + XMP_IA)
            + seg(0xEB, b"JP" + struct.pack(">H", 1) + struct.pack(">I", 1) + JUMBF)
            + seg(0xED, iptc_app13())
            + seg(0xFE, b"Criado com Midjourney COMENTARIO-INJETADO"))
    pos = 2
    if base[2:4] == b"\xff\xe0":           # depois do JFIF
        pos = 4 + struct.unpack(">H", base[4:6])[0]
    falso = b"\xff\xd8" + seg(0xE1, b"Exif\0\0" + tiff) + b"\xff\xd9"
    dados = base[:pos] + segs + base[pos:] + b"LIXO-FINAL-INJETADO" + falso
    return dados, CADEIAS_JPG_FIXAS + blobs


def jpeg_so_xmp_ia(im=None):
    base = jpeg_puro(im)
    pos = 4 + struct.unpack(">H", base[4:6])[0]
    return base[:pos] + seg(0xE1, b"http://ns.adobe.com/xap/1.0/\0" + XMP_IA) + base[pos:]


# ----------------------------------------------------------------- PNG à mão
def chunk(tipo, carga):
    return struct.pack(">I", len(carga)) + tipo + carga + struct.pack(">I", zlib.crc32(tipo + carga) & 0xFFFFFFFF)


def chunks_png(dados):
    """Devolve (lista de (tipo, bytes_brutos), sobra_depois_do_IEND)."""
    assert dados[:8] == b"\x89PNG\r\n\x1a\n", "não é PNG"
    i, saida = 8, []
    while i + 8 <= len(dados):
        tam = struct.unpack(">I", dados[i:i + 4])[0]
        tipo = dados[i + 4:i + 8]
        saida.append((tipo, dados[i:i + 12 + tam]))
        i += 12 + tam
        if tipo == b"IEND":
            break
    return saida, dados[i:]


CADEIAS_PNG_FIXAS = [
    b"Midjourney", b"SOFTWARE-INJETADO", b"trainedAlgorithmicMedia", b"DigitalSourceType",
    b"XMP-ID-INJETADO", b"c2pa", b"jumb", b"C2PA-MANIFESTO-INJETADO", b"PRIVADO-INJETADO",
    b"LIXO-PNG-INJETADO", b"XML:com.adobe.xmp",
]
TIPOS_INJETADOS_PNG = [b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME", b"caBX", b"prVt"]


def png_com_tudo(base):
    """Injeta tEXt, iTXt(XMP), zTXt, eXIf, tIME, caBX, bloco privado e lixo depois do IEND."""
    tiff, blobs = montar_exif(1, True)
    extras = (chunk(b"tEXt", b"Software\0Midjourney SOFTWARE-INJETADO")
              + chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + XMP_IA)
              + chunk(b"zTXt", b"Comment\0\0" + zlib.compress(b"ZTXT-INJETADO"))
              + chunk(b"eXIf", tiff)
              + chunk(b"tIME", struct.pack(">HBBBBB", 2031, 1, 2, 3, 4, 5))
              + chunk(b"caBX", JUMBF)
              + chunk(b"prVt", b"PRIVADO-INJETADO"))
    lista, _ = chunks_png(base)
    saida = bytearray(base[:8])
    inserido = False
    for tipo, bruto in lista:
        if tipo == b"IDAT" and not inserido:
            saida += extras
            inserido = True
        saida += bruto
    return bytes(saida) + b"LIXO-PNG-INJETADO", CADEIAS_PNG_FIXAS + blobs


# ----------------------------------------------------------- leitura própria
def marcadores_jpeg(dados):
    """Lista de (marcador, carga) dos segmentos antes do EOI e o que sobra depois dele."""
    assert dados[:2] == b"\xff\xd8", "sem SOI"
    i, n, segs = 2, len(dados), []
    while i + 1 < n:
        assert dados[i] == 0xFF, f"marcador esperado em {i}"
        while dados[i + 1] == 0xFF:
            i += 1
        m = dados[i + 1]
        if m == 0xD9:
            return segs, dados[i + 2:]
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        tam = struct.unpack(">H", dados[i + 2:i + 4])[0]
        segs.append((m, dados[i + 4:i + 2 + tam]))
        i += 2 + tam
        if m == 0xDA:
            while i + 1 < n and not (dados[i] == 0xFF and dados[i + 1] not in (0x00, 0xFF) and not 0xD0 <= dados[i + 1] <= 0xD7):
                i += 1
    raise AssertionError("JPEG sem EOI")


PROIBIDOS_JPG = {0xE1, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEF, 0xFE}


def conferir_jpeg_limpo(nome, saida, cadeias):
    faltam = presentes_em(saida, cadeias)
    checar(not faltam, f"{nome}: nenhuma cadeia injetada na saída", "ainda aparecem " + mostrar(faltam))
    try:
        segs, sobra = marcadores_jpeg(saida)
        achados = sorted({hex(m) for m, _ in segs if m in PROIBIDOS_JPG})
        checar(not achados, f"{nome}: sem segmentos APP1/APP11/APP13/COM", f"segmentos presentes: {achados}")
        checar(sobra == b"", f"{nome}: nada depois do fim da imagem", f"sobraram {len(sobra)} byte(s)")
    except AssertionError as e:
        checar(False, f"{nome}: estrutura do JPEG de saída", str(e))


def caixas_topo(dados):
    i, lista = 0, []
    while i + 8 <= len(dados):
        tam = struct.unpack(">I", dados[i:i + 4])[0]
        tipo = dados[i + 4:i + 8]
        cab = 8
        if tam == 1:
            tam = struct.unpack(">Q", dados[i + 8:i + 16])[0]
            cab = 16
        elif tam == 0:
            tam = len(dados) - i
        if tam < cab or i + tam > len(dados):
            raise AssertionError(f"caixa inválida em {i}")
        carga = dados[i + cab:i + tam]
        lista.append((tipo, carga))
        i += tam
    if i != len(dados):
        raise AssertionError("bytes soltos no fim")
    return lista


# ------------------------------------------------------------------- vídeo
def ff(*args):
    r = subprocess.run([FFMPEG, "-hide_banner", "-v", "error"] + [str(a) for a in args], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", errors="replace"), r.stderr.decode("utf-8", errors="replace")


def gerar_video(destino, tmp, tag=""):
    meta = Path(tmp) / f"meta{tag}.txt"
    meta.write_text(
        ";FFMETADATA1\ntitle=TITULO-INJETADO\ncomment=COMENTARIO-VIDEO-INJETADO c2pa\nencoder=ENCODER-INJETADO\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=CAPITULO-INJETADO\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=CAPITULO2-INJETADO\n", encoding="utf-8", newline="\n")
    for cv in (["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"], ["-c:v", "mpeg4"]):
        rc, _, err = ff("-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-i", meta,
                        "-map", "0:v", "-map", "1:a", "-map_metadata", "2", "-map_chapters", "2",
                        *cv, "-c:a", "aac", "-b:a", "64k", "-metadata:s:v:0", "handler_name=HANDLER-INJETADO",
                        "-shortest", destino)
        if rc == 0:
            break
    if rc != 0:
        raise RuntimeError("ffmpeg não gerou o vídeo de teste: " + err[:200])
    dados = Path(destino).read_bytes()
    dados += caixa(b"uuid", C2PA_UUID + b"jumb c2pa C2PA-UUID-TEXTO-INJETADO Google Veo")
    dados += caixa(b"uuid", XMP_UUID + b"<x:xmpmeta DigitalSourceType trainedAlgorithmicMedia XMP-UUID-TEXTO-INJETADO/>")
    return dados


CADEIAS_VIDEO = [
    b"TITULO-INJETADO", b"COMENTARIO-VIDEO-INJETADO", b"Lavf", b"CAPITULO-INJETADO",
    b"CAPITULO2-INJETADO", b"HANDLER-INJETADO", C2PA_UUID, XMP_UUID, b"C2PA-UUID-TEXTO-INJETADO",
    b"XMP-UUID-TEXTO-INJETADO", b"trainedAlgorithmicMedia", b"Google Veo",
]


def hashes_fluxos(caminho, sel_v="0:v", sel_v0="0:v:0", passthrough=False):
    """Hash dos pacotes (sem recodificar) de vídeo e de áudio, e dos quadros/amostras decodificados.
    sel_v/sel_v0: seletores do vídeo (para arquivo com capa embutida, use 0:V e 0:V:0: V maiúsculo não pega a capa)."""
    res = {}
    rc, out, err = ff("-i", caminho, "-map", sel_v, "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-")
    res["pacotes de vídeo"] = (rc, out.strip(), err.strip())
    rc, out, err = ff("-i", caminho, "-map", "0:a", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-")
    res["pacotes de áudio"] = (rc, out.strip(), err.strip())
    pt = ["-fps_mode", "passthrough"] if passthrough else []  # vídeo de taxa variável: sem grade fixa de quadros
    rc, out, err = ff("-i", caminho, "-map", sel_v0, *pt, "-f", "framemd5", "-")
    # framemd5: stream, dts, pts, duração, tamanho, hash; comparo só tamanho e hash
    linhas = [[c.strip() for c in l.split(",")] for l in out.splitlines() if l and not l.startswith("#")]
    res["quadros decodificados"] = (rc, [(l[4], l[5]) for l in linhas if len(l) >= 6], err.strip())
    rc, out, err = ff("-i", caminho, "-map", "0:a:0", "-f", "md5", "-")
    res["amostras de áudio decodificadas"] = (rc, out.strip(), err.strip())
    return res


def conferir_video_igual(nome, original, limpo, **sel):
    a, b = hashes_fluxos(original, **sel), hashes_fluxos(limpo, **sel)
    for k in a:
        ok_a = a[k][0] == 0 and a[k][1]
        checar(ok_a, f"{nome}: leitura de {k} do original funciona", f"rc={a[k][0]} saída={str(a[k][1])[:80]!r} erro={a[k][2][:120]}")
        checar(a[k][:2] == b[k][:2], f"{nome}: {k} idênticos entre original e limpo",
               f"original={str(a[k][1])[:110]!r} limpo={str(b[k][1])[:110]!r} erro_limpo={b[k][2][:120]}")
    conferir_pacotes(nome, original, limpo)


def _erros_normalizados(texto):
    """Linhas de erro do ffmpeg sem o endereço do objeto ([null @ 000001dbffabcf80]), que muda a cada execução."""
    return sorted({re.sub(r"@ [0-9a-fA-Fx]+", "@", l.strip()) for l in texto.splitlines() if l.strip()})


def conferir_video_abre(nome, caminho, passthrough=False, original=None):
    """Sem `original`: decodifica sem nenhuma mensagem de erro. Com `original` (vídeo de taxa variável): o ffmpeg do
    próprio teste pode reclamar do vídeo ORIGINAL ao jogar os quadros no muxer null (dois quadros caem no mesmo
    instante da grade fixa dele), e isso não é defeito do limpo; vale a regra "nenhum erro que o original não tenha"."""
    pt = ["-fps_mode", "passthrough"] if passthrough else []
    rc, _, err = ff("-i", caminho, *pt, "-f", "null", "-")
    if original is None:
        checar(rc == 0 and not err.strip(), f"{nome}: o vídeo limpo abre e decodifica sem erro", f"rc={rc} {err[:150]}")
        return
    rc0, _, err0 = ff("-i", original, *pt, "-f", "null", "-")
    novos = [e for e in _erros_normalizados(err) if e not in _erros_normalizados(err0)]
    checar(rc == 0 and rc0 == 0 and not novos, f"{nome}: o vídeo limpo abre e decodifica sem erro novo em relação ao original",
           f"rc={rc} erros novos: {novos[:2]}")


def conferir_video_limpo(nome, original_bytes, caminho, cadeias, passthrough=False, original=None):
    saida = Path(caminho).read_bytes()
    faltam = presentes_em(saida, cadeias)
    checar(not faltam, f"{nome}: nenhuma cadeia injetada na saída", "ainda aparecem " + mostrar(faltam))
    try:
        topo = caixas_topo(saida)
        uuids = [c[:16] for t, c in topo if t == b"uuid"]
        checar(C2PA_UUID not in uuids and XMP_UUID not in uuids, f"{nome}: nenhuma caixa uuid de C2PA ou de XMP no nível de topo",
               f"uuids no topo: {[u.hex() for u in uuids]}")
        checar(b"moov" in [t for t, _ in topo] and b"mdat" in [t for t, _ in topo], f"{nome}: saída tem moov e mdat",
               f"caixas: {[t for t, _ in topo]}")
    except AssertionError as e:
        checar(False, f"{nome}: estrutura de caixas da saída", str(e))
    conferir_video_abre(nome, caminho, passthrough, original)


# ------------------------------------------------------- conferência de imagem
def conferir_pixels_iguais(nome, original_bytes, caminho):
    try:
        a = pixels(original_bytes)
        b = pixels(Path(caminho).read_bytes())
    except Exception as e:
        checar(False, f"{nome}: saída abre no Pillow", repr(e))
        return
    checar(a[0] == b[0] and a[1] == b[1], f"{nome}: mesmo modo e dimensões", f"original={a[:2]} limpo={b[:2]}")
    checar(a[2] == b[2], f"{nome}: pixels idênticos aos do original")


# =================================================================== cenários
def cenario_a(T):
    print("\n--- (a) JPG com todas as marcas")
    dados, cadeias = jpeg_com_tudo()
    orig = gravar_original(T / "a" / "foto.jpg", dados)
    saida = T / "a" / "saida"
    faltam = faltando_em(dados, cadeias)
    checar(not faltam, "a: o JPG de teste realmente contém todas as cadeias injetadas", "faltam " + mostrar(faltam))
    rc, txt = rodar("analisar", orig)
    checar(rc == 0 and "[!]" in txt and "manifesto C2PA" in txt and "IPTC: mídia gerada por IA" in txt,
           "a: analisar acusa C2PA e IPTC", txt[:200])
    rc, txt = rodar("limpar", orig, "--saida", saida)
    checar(rc == 0, "a: limpar termina com código 0", f"rc={rc} {txt[-200:]}")
    checar("[ok] arquivo limpo" in txt, "a: boletim diz [ok] arquivo limpo", txt[-200:])
    destino = saida / "foto_limpo.jpg"
    if not checar(destino.is_file(), "a: arquivo foto_limpo.jpg foi criado"):
        return
    conferir_jpeg_limpo("a", destino.read_bytes(), cadeias)
    conferir_pixels_iguais("a", dados, destino)
    segs, _ = marcadores_jpeg(destino.read_bytes())
    ms = [m for m, _ in segs]
    checar(0xDA in ms and (0xC0 in ms or 0xC1 in ms or 0xC2 in ms) and 0xDB in ms, "a: continua com quadro, tabelas e varredura da imagem", str([hex(m) for m in ms]))

    # JPEG progressivo
    dados_p, cad_p = jpeg_com_tudo(progressive=True)
    orig_p = gravar_original(T / "a" / "prog.jpg", dados_p)
    rc, txt = rodar("limpar", orig_p, "--saida", saida)
    checar(rc == 0, "a2: JPG progressivo limpa com código 0", txt[-200:])
    d = saida / "prog_limpo.jpg"
    if checar(d.is_file(), "a2: prog_limpo.jpg criado"):
        conferir_jpeg_limpo("a2 progressivo", d.read_bytes(), cad_p)
        conferir_pixels_iguais("a2 progressivo", dados_p, d)

    # perfil de cor ICC em JPEG precisa ficar
    icc = perfil_icc()
    base = jpeg_puro(icc_profile=icc)
    dados_i, cad_i = jpeg_com_tudo()
    tiff, _ = montar_exif(1, True)
    pos = 4 + struct.unpack(">H", base[4:6])[0]
    comicc = base[:pos] + seg(0xE1, b"Exif\0\0" + tiff) + base[pos:]
    orig_i = gravar_original(T / "a" / "icc.jpg", comicc)
    rc, txt = rodar("limpar", orig_i, "--saida", saida)
    d = saida / "icc_limpo.jpg"
    if checar(rc == 0 and d.is_file(), "a3: JPG com ICC limpa", txt[-200:]):
        out = d.read_bytes()
        segs, _ = marcadores_jpeg(out)
        checar(any(m == 0xE2 and c.startswith(b"ICC_PROFILE") for m, c in segs), "a3: perfil de cor ICC continua no JPG")
        checar(b"CAMERA-MARCA-INJETADA" not in out, "a3: EXIF saiu")
        conferir_pixels_iguais("a3 ICC", comicc, d)


def cenario_b(T):
    print("\n--- (b) JPG com orientação EXIF diferente de 1")
    transp = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
              5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
              8: Image.Transpose.ROTATE_90}
    saida = T / "b" / "saida"
    for o in range(2, 9):
        little = (o % 2 == 0)
        dados, cadeias = jpeg_com_tudo(orient=o, little=little)
        nome = f"orient{o}"
        orig = gravar_original(T / "b" / f"{nome}.jpg", dados)
        rc, txt = rodar("limpar", orig, "--saida", saida)
        rot = f"b orientação {o} ({'II' if little else 'MM'})"
        checar(rc == 0, f"{rot}: limpar termina com código 0", txt[-250:])
        d = saida / f"{nome}_limpo.jpg"
        if not checar(d.is_file(), f"{rot}: saída criada"):
            continue
        out = d.read_bytes()
        faltam = presentes_em(out, cadeias)
        checar(not faltam, f"{rot}: nenhuma cadeia injetada na saída", "aparecem " + mostrar(faltam))
        try:
            segs, sobra = marcadores_jpeg(out)
            achados = sorted({hex(m) for m, _ in segs if m in PROIBIDOS_JPG})
            checar(not achados and sobra == b"", f"{rot}: sem APP1/APP11/APP13/COM e sem sobra", f"{achados} sobra={len(sobra)}")
        except AssertionError as e:
            checar(False, f"{rot}: estrutura do JPEG", str(e))
        try:
            with Image.open(io.BytesIO(dados)) as im0:
                esperado = im0.convert("RGB").transpose(transp[o])
            with Image.open(d) as im1:
                im1.load()
                obtido = im1.convert("RGB")
            checar(obtido.size == esperado.size, f"{rot}: dimensões da rotação aplicada", f"esperado {esperado.size}, obtido {obtido.size}")
            if obtido.size == esperado.size:
                dif = max(ImageStat.Stat(ImageChops.difference(obtido, esperado)).mean)
                checar(dif < 4.0, f"{rot}: conteúdo coincide com a rotação feita à mão", f"diferença média {dif:.2f}")
        except Exception as e:
            checar(False, f"{rot}: abrir e comparar", repr(e))


def cenario_c(T):
    print("\n--- (c) PNG")
    saida = T / "c" / "saida"
    ims = {
        "rgb": (imagem(), {}),
        "rgba": (None, {}),
        "icc": (imagem(var=30), {"icc_profile": perfil_icc()}),
        "paleta": (None, {}),
    }
    rgba = imagem(var=5).convert("RGBA")
    a = Image.new("L", rgba.size)
    pa = a.load()
    for y in range(rgba.size[1]):
        for x in range(rgba.size[0]):
            pa[x, y] = (x * 255 // rgba.size[0])
    rgba.putalpha(a)
    ims["rgba"] = (rgba, {})
    pal = imagem(var=9).convert("P", palette=Image.Palette.ADAPTIVE, colors=32)
    ims["paleta"] = (pal, {"transparency": 0})
    for nome, (im, kw) in ims.items():
        base = png_puro(im, **kw)
        tipos_base = [t for t, _ in chunks_png(base)[0]]
        dados, cadeias = png_com_tudo(base)
        orig = gravar_original(T / "c" / f"{nome}.png", dados)
        rot = f"c PNG {nome}"
        faltam = faltando_em(dados, cadeias)
        checar(not faltam, f"{rot}: o PNG de teste contém as cadeias injetadas", "faltam " + mostrar(faltam))
        rc, txt = rodar("limpar", orig, "--saida", saida)
        checar(rc == 0, f"{rot}: limpar termina com código 0", txt[-250:])
        d = saida / f"{nome}_limpo.png"
        if not checar(d.is_file(), f"{rot}: saída criada"):
            continue
        out = d.read_bytes()
        faltam = presentes_em(out, cadeias)
        checar(not faltam, f"{rot}: nenhuma cadeia injetada na saída", "aparecem " + mostrar(faltam))
        try:
            lista, sobra = chunks_png(out)
            tipos = [t for t, _ in lista]
            inj = [t.decode() for t in tipos if t in TIPOS_INJETADOS_PNG]
            checar(not inj, f"{rot}: sem tEXt/iTXt/zTXt/eXIf/tIME/caBX/privado", f"presentes: {inj}")
            checar(sobra == b"", f"{rot}: nada depois do IEND", f"sobraram {len(sobra)} byte(s)")
            checar(tipos == tipos_base, f"{rot}: restam exatamente os blocos de imagem e cor do original",
                   f"esperado {[t.decode() for t in tipos_base]} obtido {[t.decode() for t in tipos]}")
        except AssertionError as e:
            checar(False, f"{rot}: estrutura do PNG", str(e))
            continue
        conferir_pixels_iguais(rot, dados, d)
        with Image.open(d) as im1:
            im1.load()
            if nome == "rgba":
                ext = im1.getchannel("A").getextrema() if im1.mode == "RGBA" else None
                checar(ext is not None and ext[0] < 10 and ext[1] > 200, f"{rot}: transparência preservada", f"modo={im1.mode} alfa={ext}")
            if nome == "paleta":
                checar("transparency" in im1.info, f"{rot}: bloco tRNS preservado")
        if nome == "icc":
            def icc_de(b):
                for t, bruto in chunks_png(b)[0]:
                    if t == b"iCCP":
                        carga = bruto[8:-4]
                        nome_fim = carga.index(b"\0")
                        return zlib.decompress(carga[nome_fim + 2:])
                return None
            checar(icc_de(base) is not None and icc_de(out) == icc_de(base), f"{rot}: perfil de cor iCCP preservado e idêntico")


def cenario_d(T):
    print("\n--- (d) MP4 e MOV")
    if not FFMPEG:
        PULADOS.append("d")
        print("[AVISO] ffmpeg não encontrado no PATH: testes de vídeo PULADOS (isso NÃO conta como passar em vídeo)")
        return
    saida = T / "d" / "saida"
    for ext in (".mp4", ".mov"):
        nome = "video" + ext
        rot = f"d {ext[1:].upper()}"
        orig_tmp = T / "d" / ("gerado" + ext)
        orig_tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            dados = gerar_video(str(orig_tmp), T / "d")
        except Exception as e:
            checar(False, f"{rot}: gerar vídeo de teste", str(e))
            continue
        orig_tmp.unlink()
        orig = gravar_original(T / "d" / nome, dados)
        faltam = faltando_em(dados, CADEIAS_VIDEO)
        checar(not faltam, f"{rot}: o vídeo de teste contém todas as cadeias injetadas", "faltam " + mostrar(faltam))
        rc, txt = rodar("analisar", orig)
        checar(rc == 0 and "[!]" in txt and "caixa uuid do C2PA" in txt, f"{rot}: analisar acusa a caixa uuid do C2PA", txt[:200])
        rc, txt = rodar("limpar", orig, "--saida", saida)
        checar(rc == 0, f"{rot}: limpar termina com código 0", txt[-250:])
        checar("[ok] arquivo limpo" in txt, f"{rot}: boletim diz [ok] arquivo limpo", txt[-250:])
        d = saida / f"video_limpo{ext}"
        if not checar(d.is_file(), f"{rot}: saída criada"):
            continue
        conferir_video_limpo(rot, dados, str(d), CADEIAS_VIDEO)
        conferir_video_igual(rot, orig, str(d))


def cenario_e(T):
    print("\n--- (e) lote de tipos mistos")
    pasta = T / "e"
    saida = pasta / "saida"
    arquivos = []
    dj, cj = jpeg_com_tudo()
    arquivos.append((gravar_original(pasta / "com-marcas.jpg", dj), cj, "jpeg"))
    sem = jpeg_puro(imagem(var=77))
    arquivos.append((gravar_original(pasta / "sem-marca.jpg", sem), [], "jpeg"))
    ia = jpeg_so_xmp_ia(imagem(var=120))
    arquivos.append((gravar_original(pasta / "declarada-ia.jpg", ia), [b"trainedAlgorithmicMedia", b"DigitalSourceType", b"XMP-ID-INJETADO"], "jpeg"))
    dp, cp = png_com_tudo(png_puro(imagem(var=44)))
    arquivos.append((gravar_original(pasta / "imagem-ia.png", dp), cp, "png"))
    pl = png_puro(imagem(var=3))
    arquivos.append((gravar_original(pasta / "sem-marca.png", pl), [], "png"))
    if FFMPEG:
        for ext in (".mp4", ".mov"):
            tmp = pasta / ("g" + ext)
            try:
                dv = gerar_video(str(tmp), pasta, tag=ext[1:])
                tmp.unlink()
                arquivos.append((gravar_original(pasta / ("clipe" + ext), dv), CADEIAS_VIDEO, "video"))
            except Exception as e:
                checar(False, f"e: gerar vídeo {ext}", str(e))
    else:
        PULADOS.append("e (vídeo)")
        print("[AVISO] ffmpeg ausente: o lote roda sem os vídeos")
    n = len(arquivos)
    rc, txt = rodar("limpar", *[a[0] for a in arquivos], "--saida", saida)
    checar(rc == 0, "e: lote termina com código 0", f"rc={rc}")
    esperado = f"Resumo: {n} de {n} arquivo(s) limpo(s)"
    checar(ultima_linha_util(txt) == esperado, "e: última linha útil é o resumo do lote", f"última linha: {ultima_linha_util(txt)!r}, esperado {esperado!r}")
    for caminho, cadeias, tipo in arquivos:
        p = Path(caminho)
        d = saida / f"{p.stem}_limpo{p.suffix}"
        rot = f"e {p.name}"
        if not checar(d.is_file(), f"{rot}: saída criada"):
            continue
        out = d.read_bytes()
        faltam = presentes_em(out, cadeias)
        checar(not faltam, f"{rot}: nenhuma cadeia injetada na saída", "aparecem " + mostrar(faltam))
        if tipo == "jpeg":
            conferir_jpeg_limpo(rot, out, cadeias)
            conferir_pixels_iguais(rot, p.read_bytes(), d)
        elif tipo == "png":
            conferir_pixels_iguais(rot, p.read_bytes(), d)
            try:
                tipos = [t for t, _ in chunks_png(out)[0]]
                checar(not [t for t in tipos if t in TIPOS_INJETADOS_PNG], f"{rot}: sem blocos de metadado", str(tipos))
            except AssertionError as e:
                checar(False, f"{rot}: estrutura do PNG", str(e))
        else:
            conferir_video_abre(rot, str(d))
            conferir_video_igual(rot, str(p), str(d))
    rc, txt = rodar("analisar", str(pasta / "sem-marca.jpg"))
    checar(rc == 0 and "[ok]" in txt and "nenhuma marca" in txt, "e: analisar de arquivo sem marca diz nenhuma marca", txt[:200])


def cenario_f(T):
    print("\n--- (f) mesmo nome em pastas diferentes, mesma --saida")
    saida = T / "f" / "saida"
    for ext, fazer in ((".jpg", lambda v: jpeg_com_tudo(imagem(var=v))[0]), (".png", lambda v: png_com_tudo(png_puro(imagem(var=v)))[0])):
        a = gravar_original(T / "f" / "p1" / f"foto{ext}", fazer(10))
        b = gravar_original(T / "f" / "p2" / f"foto{ext}", fazer(150))
        checar(pixels(Path(a).read_bytes())[2] != pixels(Path(b).read_bytes())[2], f"f {ext}: os dois originais são mesmo diferentes")
        rc, txt = rodar("limpar", a, b, "--saida", saida)
        checar(rc == 0, f"f {ext}: limpar termina com código 0", txt[-250:])
        arqs = sorted(p.name for p in saida.glob(f"foto*{ext}"))
        checar(len(arqs) == 2, f"f {ext}: saem dois arquivos distintos", f"arquivos: {arqs}")
        if len(arqs) == 2:
            pix = {p.name: pixels(p.read_bytes())[2] for p in saida.glob(f"foto*{ext}")}
            pa, pb = pixels(Path(a).read_bytes())[2], pixels(Path(b).read_bytes())[2]
            checar(sorted(pix.values()) == sorted([pa, pb]), f"f {ext}: cada saída tem o conteúdo de um original diferente")
            primeiro = saida / f"foto_limpo{ext}"
            if primeiro.is_file():
                checar(pixels(primeiro.read_bytes())[2] == pa, f"f {ext}: foto_limpo{ext} é a cópia do primeiro pedido")
                outro = [p for p in saida.glob(f"foto*{ext}") if p != primeiro][0]
                checar(pixels(outro.read_bytes())[2] == pb, f"f {ext}: o segundo nome é a cópia do segundo pedido ({outro.name})")
        shutil.rmtree(saida, ignore_errors=True)


def cenario_g(T):
    print("\n--- (g) formato não aceito e arquivo inexistente no meio do lote")
    pasta = T / "g"
    saida = pasta / "saida"
    webp = gravar_original(pasta / "falso.webp", b"RIFF" + struct.pack("<I", 28) + b"WEBPVP8 " + b"\0" * 20)
    heic = gravar_original(pasta / "falso.heic", struct.pack(">I", 24) + b"ftypheic" + b"\0\0\0\0" + b"mif1heic")
    sumiu = str(pasta / "nao-existe.jpg")
    bom_j = gravar_original(pasta / "bom.jpg", jpeg_com_tudo()[0])
    bom_p = gravar_original(pasta / "bom.png", png_com_tudo(png_puro())[0])
    rc, txt = rodar("limpar", webp, heic, sumiu, bom_j, bom_p, "--saida", saida)
    checar(rc != 0, "g: código de saída diferente de 0", f"rc={rc}")
    resumos = [l.strip() for l in txt.splitlines() if l.strip().startswith("Resumo:")]
    checar(resumos == ["Resumo: 2 de 5 arquivo(s) limpo(s)"], "g: resumo diz 2 de 5", f"linhas de resumo: {resumos}")
    for nome in ("falso.webp", "falso.heic", "nao-existe.jpg"):
        checar(any(l.strip().startswith("não limpo:") and nome in l for l in txt.splitlines()), f"g: {nome} aparece como não limpo", txt[-400:])
    checar(txt.count("[?]") >= 2, "g: WebP e HEIC recebem o aviso [?] de formato não suportado", txt[:300])
    checar("não encontrado" in txt, "g: arquivo inexistente é dito como não encontrado")
    for nome in ("bom_limpo.jpg", "bom_limpo.png"):
        d = saida / nome
        if checar(d.is_file(), f"g: {nome} foi limpo apesar dos outros falharem"):
            out = d.read_bytes()
            checar(b"c2pa" not in out and b"Midjourney" not in out and b"trainedAlgorithmicMedia" not in out, f"g: {nome} sem marcas")
    extras = sorted(p.name for p in saida.iterdir()) if saida.is_dir() else []
    checar(extras == ["bom_limpo.jpg", "bom_limpo.png"], "g: nenhum arquivo de saída criado para os não aceitos", f"saída tem {extras}")


def cenario_i_j_k(T):
    print("\n--- (i) sem --saida, (j) nome com espaço e acento, (k) rodar duas vezes")
    pasta = T / "i"
    j = gravar_original(pasta / "foto.jpg", jpeg_com_tudo()[0])
    p = gravar_original(pasta / "arte.png", png_com_tudo(png_puro())[0])
    alvos = [j, p]
    video_ok = False
    if FFMPEG:
        tmp = pasta / "tmp.mp4"
        try:
            dv = gerar_video(str(tmp), pasta, tag="i")
            tmp.unlink()
            alvos.append(gravar_original(pasta / "video.mp4", dv))
            video_ok = True
        except Exception as e:
            checar(False, "i: gerar vídeo", str(e))
    else:
        PULADOS.append("i (vídeo)")
    antes = sorted(x.name for x in pasta.iterdir())
    rc, txt = rodar("limpar", *alvos)
    checar(rc == 0, "i: sem --saida, limpar termina com código 0", txt[-250:])
    for a in alvos:
        pa = Path(a)
        d = pa.parent / "limpos" / f"{pa.stem}_limpo{pa.suffix}"
        checar(d.is_file(), f"i: cópia em limpos/{d.name} ao lado do original")
    depois = sorted(x.name for x in pasta.iterdir())
    checar(depois == sorted(antes + ["limpos"]), "i: a pasta do original só ganhou a subpasta limpos", f"antes={antes} depois={depois}")
    checar(sorted(x.name for x in (pasta / "limpos").iterdir()) == sorted(f"{Path(a).stem}_limpo{Path(a).suffix}" for a in alvos),
           "i: limpos contém só as cópias com sufixo _limpo")

    # (j) nome com espaço e acento, e pasta de saída também com espaço e acento
    pj = T / "j"
    nomes = ["foto de férias ção.jpg", "capa à direita ñ.png"]
    caminhos = [gravar_original(pj / nomes[0], jpeg_com_tudo()[0]), gravar_original(pj / nomes[1], png_com_tudo(png_puro())[0])]
    if video_ok:
        tmp = pj / "t.mp4"
        dv = gerar_video(str(tmp), pj, tag="j")
        tmp.unlink()
        caminhos.append(gravar_original(pj / "vídeo ação final.mp4", dv))
    rc, txt = rodar("limpar", *caminhos)
    checar(rc == 0, "j: nomes com espaço e acento, sem --saida: código 0", txt[-300:])
    for c in caminhos:
        pc = Path(c)
        checar((pc.parent / "limpos" / f"{pc.stem}_limpo{pc.suffix}").is_file(), f"j: saída existe para {pc.name}")
    saida = pj / "pasta de saída ção"
    rc, txt = rodar("limpar", *caminhos, "--saida", saida)
    checar(rc == 0, "j: --saida com espaço e acento: código 0", txt[-300:])
    for c in caminhos:
        pc = Path(c)
        d = saida / f"{pc.stem}_limpo{pc.suffix}"
        checar(d.is_file(), f"j: saída em pasta com acento existe para {pc.name}")
        if d.is_file() and pc.suffix in (".jpg", ".png"):
            conferir_pixels_iguais(f"j {pc.name}", pc.read_bytes(), d)
        elif d.is_file():
            conferir_video_igual(f"j {pc.name}", str(pc), str(d))

    # (k) rodar duas vezes: a segunda sobrescreve a cópia
    pk = T / "k"
    ck = gravar_original(pk / "foto.jpg", jpeg_com_tudo(imagem(var=60))[0])
    s = pk / "saida"
    rc1, t1 = rodar("limpar", ck, "--saida", s)
    d = s / "foto_limpo.jpg"
    checar(rc1 == 0 and d.is_file(), "k: primeira limpeza funciona", t1[-200:])
    d.write_bytes(b"LIXO QUE A SEGUNDA RODADA TEM DE SOBRESCREVER")
    rc2, t2 = rodar("limpar", ck, "--saida", s)
    checar(rc2 == 0, "k: segunda limpeza termina com código 0", t2[-200:])
    checar(sorted(x.name for x in s.iterdir()) == ["foto_limpo.jpg"], "k: segunda rodada sobrescreve, sem criar _limpo_2", str(sorted(x.name for x in s.iterdir())))
    out = d.read_bytes()
    checar(out[:2] == b"\xff\xd8" and b"c2pa" not in out, "k: a cópia foi regravada com o conteúdo limpo")
    conferir_pixels_iguais("k segunda rodada", Path(ck).read_bytes(), d)
    # e sem --saida
    rc3, _ = rodar("limpar", ck)
    rc4, t4 = rodar("limpar", ck)
    checar(rc3 == 0 and rc4 == 0 and sorted(x.name for x in (pk / "limpos").iterdir()) == ["foto_limpo.jpg"],
           "k: duas rodadas sem --saida também funcionam e sobrescrevem", t4[-200:])


# ============================================================ casos novos (l a q)
FFPROBE = shutil.which("ffprobe")


def ffprobe_fluxos(caminho):
    """Lista de fluxos (dict do ffprobe) do arquivo; None se não deu para ler."""
    if not FFPROBE:
        return None
    r = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(caminho)], capture_output=True)
    if r.returncode != 0:
        return None
    import json
    return json.loads(r.stdout.decode("utf-8", errors="replace")).get("streams", [])


def pacotes_por_fluxo(caminho):
    """Leitura própria dos pacotes pelo ffprobe, sem decodificar. Devolve {(tipo, ordem): dict} com a base de tempo do
    fluxo e, por pacote na ordem do arquivo, (pts em segundos exatos, dts em segundos exatos, tamanho, hash dos dados).
    pts e dts vêm como inteiros e são multiplicados pela base de tempo do PRÓPRIO fluxo (Fraction, sem arredondar), então
    arquivos com escalas de tempo diferentes (1/90000, 1/1000, 1/15360...) ficam comparáveis. A capa embutida não entra.
    None se não deu para ler."""
    if not FFPROBE:
        return None
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                        "packet=stream_index,pts,dts,size,data_hash:stream=index,codec_type,time_base:stream_disposition=attached_pic",
                        "-show_data_hash", "sha256", "-of", "json", str(caminho)], capture_output=True)
    if r.returncode != 0:
        return None
    js = json.loads(r.stdout.decode("utf-8", errors="replace"))
    chave, saida, cont = {}, {}, {}
    for f in js.get("streams", []):
        if f.get("codec_type") not in ("video", "audio") or com_capa(f):
            continue
        n = cont.get(f["codec_type"], 0)
        cont[f["codec_type"]] = n + 1
        chave[f["index"]] = (f["codec_type"], n)
        saida[(f["codec_type"], n)] = {"base": Fraction(f["time_base"]), "pacotes": []}
    for p in js.get("packets", []):
        k = chave.get(p.get("stream_index"))
        if k is None:
            continue
        tb = saida[k]["base"]
        pts = Fraction(p["pts"]) * tb if p.get("pts") is not None else None
        dts = Fraction(p["dts"]) * tb if p.get("dts") is not None else None
        saida[k]["pacotes"].append((pts, dts, int(p["size"]), p.get("data_hash")))
    return saida


def conferir_pacotes(nome, original, limpo, minimo=1):
    """Pacote a pacote e por fluxo: tempo de apresentação, tempo de decodificação, tamanho e hash dos dados iguais."""
    a, b = pacotes_por_fluxo(original), pacotes_por_fluxo(limpo)
    if not checar(a is not None and b is not None and len(a) >= 1,
                  f"{nome}: ffprobe lê os pacotes do original e do limpo", f"original={'ok' if a else a} limpo={'ok' if b else b}"):
        return
    checar(sorted(a) == sorted(b), f"{nome}: mesmos fluxos (tipo e ordem) no original e no limpo", f"original={sorted(a)} limpo={sorted(b)}")
    for k in sorted(a):
        if k not in b:
            continue
        pa, pb = a[k]["pacotes"], b[k]["pacotes"]
        rot = f"{nome}: fluxo {k[0]} {k[1]}"
        checar(len(pa) >= minimo, f"{rot}: o original tem pacotes para comparar", f"{len(pa)} pacote(s)")
        checar(len(pa) == len(pb), f"{rot}: mesmo número de pacotes", f"original={len(pa)} limpo={len(pb)}")
        for i, campo in enumerate(("tempo de apresentação (pts, em segundos)", "tempo de decodificação (dts, em segundos)", "tamanho", "hash dos dados")):
            va, vb = [x[i] for x in pa], [x[i] for x in pb]
            dif = next((j for j in range(min(len(va), len(vb))) if va[j] != vb[j]), None)
            det = "" if dif is None else f"primeira diferença no pacote {dif}: original={str(va[dif])} limpo={str(vb[dif])}"
            checar(va == vb, f"{rot}: {campo} idêntico pacote a pacote", det or f"{len(va)} x {len(vb)} pacotes")


def com_capa(fluxo):
    return bool(fluxo.get("disposition", {}).get("attached_pic"))


def mp4_trocar_caixa(dados, caminho, nova, ini=0, fim=None):
    """Refaz a árvore de caixas trocando o corpo da primeira caixa em `caminho` (ex.: [b"moov", b"trak"])
    por nova(corpo). Só reescreve tamanhos; serve para arquivo com o moov DEPOIS do mdat (sem faststart),
    onde mexer no moov não desloca os offsets das amostras."""
    fim = len(dados) if fim is None else fim
    saida, feito, i = bytearray(), False, ini
    while i + 8 <= fim:
        tam = struct.unpack(">I", dados[i:i + 4])[0]
        tipo = dados[i + 4:i + 8]
        if not feito and tipo == caminho[0]:
            corpo = dados[i + 8:i + tam]
            novo = nova(corpo) if len(caminho) == 1 else mp4_trocar_caixa(corpo, caminho[1:], nova)
            saida += struct.pack(">I", 8 + len(novo)) + tipo + novo
            feito = True
        else:
            saida += dados[i:i + tam]
        i += tam
    assert feito, f"caixa {caminho[0]!r} não encontrada"
    return bytes(saida)


def mp4_minimo(moov_extra=b"", topo_extra=b"", marca=b"isom"):
    """MP4 de mentira (só estrutura, não toca) para testar a LEITURA de caixas sem depender do ffmpeg."""
    ftyp = caixa(b"ftyp", marca + b"\0\0\2\0" + b"isomiso2")
    moov = caixa(b"moov", caixa(b"mvhd", b"\0" * 100) + moov_extra)
    return ftyp + moov + caixa(b"mdat", b"\0" * 64) + topo_extra


def inserir_chunk_png(dados, tipo, carga, antes=b"IDAT"):
    """Põe um bloco PNG antes do primeiro bloco `antes`."""
    lista, sobra = chunks_png(dados)
    saida, feito = bytearray(dados[:8]), False
    for t, bruto in lista:
        if t == antes and not feito:
            saida += chunk(tipo, carga)
            feito = True
        saida += bruto
    assert feito
    return bytes(saida) + sobra


def imagem_comprimida(dados):
    """Bytes da imagem JPEG sem nenhum APPn/COM: tabelas, quadro e dados comprimidos da varredura, até o EOI."""
    assert dados[:2] == b"\xff\xd8"
    i, n, saida = 2, len(dados), bytearray(b"\xff\xd8")
    while i + 1 < n:
        assert dados[i] == 0xFF
        while dados[i + 1] == 0xFF:
            i += 1
        m = dados[i + 1]
        if m == 0xD9:
            return bytes(saida + b"\xff\xd9")
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            saida += dados[i:i + 2]
            i += 2
            continue
        tam = struct.unpack(">H", dados[i + 2:i + 4])[0]
        bloco = dados[i:i + 2 + tam]
        i += 2 + tam
        if not (0xE0 <= m <= 0xEF or m == 0xFE):
            saida += bloco
        if m == 0xDA:
            j = i
            while j + 1 < n and not (dados[j] == 0xFF and dados[j + 1] not in (0x00, 0xFF) and not 0xD0 <= dados[j + 1] <= 0xD7):
                j += 1
            saida += dados[i:j]
            i = j
    raise AssertionError("JPEG sem EOI")


def carregar_modulo():
    """EXCEÇÃO À REGRA DE INDEPENDÊNCIA (só no caso o): importa o script testado para chamar direto a
    função de conferência de saída, que a linha de comando não expõe sozinha."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("limpar_metadados_sob_teste", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ (l) capa embutida
def video_com_capa(tmp, ext, tag=""):
    """Vídeo com tags, capítulos, caixas uuid E uma capa JPEG embutida (fluxo mjpeg com disposition
    attached_pic) que carrega EXIF, XMP com marca de IA e bloco C2PA. Devolve (bytes, cadeias)."""
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    capa, cad_capa = jpeg_com_tudo()
    (tmp / f"capa{tag}.jpg").write_bytes(capa)
    meta = tmp / f"metacapa{tag}.txt"
    meta.write_text(
        ";FFMETADATA1\ntitle=TITULO-INJETADO\ncomment=COMENTARIO-VIDEO-INJETADO c2pa\nencoder=ENCODER-INJETADO\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=CAPITULO-INJETADO\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=CAPITULO2-INJETADO\n", encoding="utf-8", newline="\n")
    base = tmp / f"base{tag}.mp4"
    for cv in (["-c:v:0", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"], ["-c:v:0", "mpeg4"]):
        # sem -shortest: a capa é uma imagem de 1 quadro e cortaria o vídeo
        rc, _, err = ff("-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-i", tmp / f"capa{tag}.jpg", "-i", meta,
                        "-map", "0:v", "-map", "1:a", "-map", "2:v", "-map_metadata", "3", "-map_chapters", "3",
                        *cv, "-c:a", "aac", "-b:a", "64k", "-c:v:1", "copy", "-disposition:v:1", "attached_pic",
                        "-metadata:s:v:0", "handler_name=HANDLER-INJETADO", "-f", "mp4", base)
        if rc == 0:
            break
    if rc != 0:
        raise RuntimeError("ffmpeg não gerou o vídeo com capa: " + err[:200])
    dados = base.read_bytes()
    base.unlink()
    if ext == ".mov":
        # o muxer MOV do ffmpeg descarta a capa; então o MOV de teste é o MP4 com a capa trocado para marca "qt  "
        # (a capa vai na caixa covr, que o leitor de MOV do ffmpeg também lê como attached_pic)
        assert dados[4:8] == b"ftyp" and dados[16:20] == b"isom"
        dados = dados[:8] + b"qt  " + dados[12:16] + b"qt  " + dados[20:]
    dados += caixa(b"uuid", C2PA_UUID + b"jumb c2pa C2PA-UUID-TEXTO-INJETADO Google Veo")
    dados += caixa(b"uuid", XMP_UUID + b"<x:xmpmeta DigitalSourceType trainedAlgorithmicMedia XMP-UUID-TEXTO-INJETADO/>")
    return dados, CADEIAS_VIDEO + cad_capa


def cenario_l(T):
    print("\n--- (l) MP4 e MOV com capa embutida (attached_pic) carregando EXIF, XMP de IA e C2PA")
    if not FFMPEG or not FFPROBE:
        PULADOS.append("l")
        print("[AVISO] ffmpeg/ffprobe ausentes: testes de capa PULADOS")
        return
    saida = T / "l" / "saida"
    for ext in (".mp4", ".mov"):
        rot = f"l {ext[1:].upper()} com capa"
        try:
            dados, cadeias = video_com_capa(T / "l", ext, tag=ext[1:])
        except Exception as e:
            checar(False, f"{rot}: gerar vídeo de teste", str(e))
            continue
        orig = gravar_original(T / "l" / f"clipe-capa{ext}", dados)
        faltam = faltando_em(dados, cadeias)
        checar(not faltam, f"{rot}: o original contém todas as cadeias injetadas (inclusive as da capa)", "faltam " + mostrar(faltam))
        fl = ffprobe_fluxos(orig)
        checar(fl is not None and sum(com_capa(f) for f in fl) == 1 and any(f["codec_type"] == "audio" for f in fl),
               f"{rot}: ffprobe confirma 1 fluxo de capa (attached_pic) no original", str([(f.get("codec_name"), com_capa(f)) for f in fl or []]))
        rc, txt = rodar("analisar", orig)
        checar(rc == 0 and "[!]" in txt and "manifesto C2PA" in txt, f"{rot}: analisar acusa a marca da capa", txt[:200])
        rc, txt = rodar("limpar", orig, "--saida", saida)
        checar(rc == 0, f"{rot}: limpar termina com código 0", txt[-250:])
        checar("[ok] arquivo limpo" in txt, f"{rot}: boletim diz [ok] arquivo limpo", txt[-250:])
        checar(ultima_linha_util(txt) == "Resumo: 1 de 1 arquivo(s) limpo(s)", f"{rot}: resumo 1 de 1", ultima_linha_util(txt))
        d = saida / f"clipe-capa_limpo{ext}"
        if not checar(d.is_file(), f"{rot}: saída criada"):
            continue
        fo = ffprobe_fluxos(d)
        checar(fo is not None and not any(com_capa(f) for f in fo), f"{rot}: ffprobe não acha fluxo de capa na saída",
               str([(f.get("codec_name"), com_capa(f)) for f in fo or []]))
        checar(fo is not None and sorted(f["codec_type"] for f in fo) == ["audio", "video"] and not any(f.get("codec_name") == "mjpeg" for f in fo),
               f"{rot}: a saída tem só 1 vídeo e 1 áudio (sem mjpeg)", str([(f.get("codec_type"), f.get("codec_name")) for f in fo or []]))
        conferir_video_limpo(rot, dados, str(d), cadeias)
        conferir_video_igual(rot + " (vídeo e áudio principais)", orig, str(d), sel_v="0:V", sel_v0="0:V:0")


# ------------------------------------------------------- (m) original protegido
def cenario_m(T):
    print("\n--- (m) original protegido: foto.* e foto_limpo.* diferentes, no mesmo pedido")
    for ext, fazer in ((".jpg", lambda v: jpeg_com_tudo(imagem(var=v))[0]), (".png", lambda v: png_com_tudo(png_puro(imagem(var=v)))[0])):
        # m1: --saida aponta para a pasta dos dois originais
        pasta = T / "m1" / ext[1:]
        a = gravar_original(pasta / f"foto{ext}", fazer(20))
        b = gravar_original(pasta / f"foto_limpo{ext}", fazer(170))
        pa, pb = pixels(Path(a).read_bytes())[2], pixels(Path(b).read_bytes())[2]
        checar(pa != pb, f"m1 {ext}: os dois originais são mesmo diferentes")
        rc, txt = rodar("limpar", a, b, "--saida", pasta)
        checar(rc == 0, f"m1 {ext}: limpar termina com código 0", txt[-250:])
        checar(ultima_linha_util(txt) == "Resumo: 2 de 2 arquivo(s) limpo(s)", f"m1 {ext}: o resumo diz 2 de 2", ultima_linha_util(txt))
        for o, h in ((a, HASHES[a]), (b, HASHES[b])):
            checar(Path(o).is_file() and sha(o) == h, f"m1 {ext}: {Path(o).name} manteve o sha256")
        novos = sorted(p for p in pasta.rglob("*") if p.is_file() and str(p) not in (a, b))
        checar(len(novos) == 2, f"m1 {ext}: saem duas cópias novas, além dos dois originais", f"novos: {[p.name for p in novos]}")
        if len(novos) == 2:
            pix = [pixels(p.read_bytes())[2] for p in novos]
            checar(sorted(pix) == sorted([pa, pb]), f"m1 {ext}: cada cópia tem os pixels de um original diferente")
            for p in novos:
                out = p.read_bytes()
                checar(b"c2pa" not in out and b"Midjourney" not in out and b"trainedAlgorithmicMedia" not in out, f"m1 {ext}: {p.name} sem marcas")

        # m2: sem --saida; o segundo original já mora em limpos/ e leva o nome que a primeira cópia ganharia
        pasta = T / "m2" / ext[1:] / "x"
        a = gravar_original(pasta / f"foto{ext}", fazer(30))
        b = gravar_original(pasta / "limpos" / f"foto_limpo{ext}", fazer(160))
        pa, pb = pixels(Path(a).read_bytes())[2], pixels(Path(b).read_bytes())[2]
        checar(pa != pb, f"m2 {ext}: os dois originais são mesmo diferentes")
        rc, txt = rodar("limpar", a, b)
        checar(rc == 0, f"m2 {ext}: sem --saida, limpar termina com código 0", txt[-250:])
        checar(ultima_linha_util(txt) == "Resumo: 2 de 2 arquivo(s) limpo(s)", f"m2 {ext}: o resumo diz 2 de 2", ultima_linha_util(txt))
        for o, h in ((a, HASHES[a]), (b, HASHES[b])):
            checar(Path(o).is_file() and sha(o) == h, f"m2 {ext}: {Path(o).name} manteve o sha256")
        novos = sorted(p for p in pasta.rglob("*") if p.is_file() and str(p) not in (a, b))
        checar(len(novos) == 2, f"m2 {ext}: saem duas cópias novas, além dos dois originais", f"novos: {[str(p.relative_to(pasta)) for p in novos]}")
        if len(novos) == 2:
            pix = [pixels(p.read_bytes())[2] for p in novos]
            checar(sorted(pix) == sorted([pa, pb]), f"m2 {ext}: cada cópia tem os pixels de um original diferente")
            checar(len({p.read_bytes() for p in novos}) == 2, f"m2 {ext}: as duas cópias são arquivos distintos")


# ------------------------------------------------------------- (n) sem alarme falso
def png_paleta(cores, extra=0):
    """PNG de paleta SEM metadado. `cores` = lista de (r, g, b) nas posições 0..; as demais posições ganham cores neutras."""
    n = max(4, len(cores) + extra)
    paleta = list(cores) + [(200 + i, 10, 10 + i) for i in range(n - len(cores))]
    im = Image.new("P", (96, 64))
    im.putpalette([c for rgb in paleta for c in rgb])
    px = im.load()
    for y in range(64):
        for x in range(96):
            px[x, y] = ((x // 12) + (y // 8)) % n
    return png_puro(im), paleta


def jpeg_com_icc_binario(carga_extra):
    base = jpeg_puro()
    pos = 4 + struct.unpack(">H", base[4:6])[0]
    return base[:pos] + seg(0xE2, b"ICC_PROFILE\0\1\1" + perfil_icc() + carga_extra) + base[pos:]


def mp4_com_bytes_na_tabela(tmp):
    """MP4 válido em que 'Veo', 'c2pa' e 'jumb' aparecem dentro da tabela stts (durações das amostras), em
    entradas de contagem 0 (não alteram o tempo de nenhum quadro). Valores: 0x0056656F = 00 'Veo'; 0x63327061 = 'c2pa';
    0x6A756D62 = 'jumb'."""
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    base = tmp / "base-stts.mp4"
    for cv in (["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"], ["-c:v", "mpeg4"]):
        rc, _, err = ff("-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-map", "0:v", "-map", "1:a",
                        *cv, "-c:a", "aac", "-b:a", "64k", "-shortest", base)
        if rc == 0:
            break
    if rc != 0:
        raise RuntimeError("ffmpeg não gerou o vídeo base: " + err[:200])
    dados = base.read_bytes()
    base.unlink()
    caixas = [(dados[i + 4:i + 8]) for i in _topo(dados)]
    assert caixas.index(b"mdat") < caixas.index(b"moov"), f"moov antes do mdat: {caixas}"

    def novo_stts(corpo):
        n = struct.unpack(">I", corpo[4:8])[0]
        total = sum(struct.unpack(">I", corpo[8 + 8 * k:12 + 8 * k])[0] for k in range(n))
        # entradas com contagem 0 não mudam a duração de nenhuma amostra: a imagem decodificada é a mesma
        ent = [(total, 1024), (0, 0x0056656F), (0, 0x63327061), (0, 0x6A756D62)]
        return corpo[:4] + struct.pack(">I", len(ent)) + b"".join(struct.pack(">II", a, b) for a, b in ent)

    return mp4_trocar_caixa(dados, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stts"], novo_stts)


def _topo(dados):
    i = 0
    while i + 8 <= len(dados):
        yield i
        i += struct.unpack(">I", dados[i:i + 4])[0]


def cenario_n(T):
    print("\n--- (n) sem alarme falso: bytes 'Veo', 'c2pa', 'jumb' em região binária legítima")
    pasta = T / "n"
    saida = pasta / "saida"
    # PNG de paleta sem metadado, com a cor #56656F (bytes 56 65 6F = "Veo") entre bytes que não são letras
    png_veo, paleta = png_paleta([(0, 0, 0), (0x56, 0x65, 0x6F), (0, 0, 0)])
    tipos = [t for t, _ in chunks_png(png_veo)[0]]
    checar(tipos == [b"IHDR", b"PLTE", b"IDAT", b"IEND"], "n: o PNG de paleta não tem nenhum metadado", str(tipos))
    checar(bytes([0, 0, 0, 0x56, 0x65, 0x6F, 0, 0, 0]) in png_veo, "n: a paleta do PNG tem os bytes 00 00 00 56 65 6F 00 00 00")
    # PNG com tRNS cujos bytes contêm c2pa e jumb (alfa 0xFF em volta)
    png_t, _ = png_paleta([(0, 0, 0)] * 2, extra=14)
    png_trns = inserir_chunk_png(png_t, b"tRNS", b"\xffc2pa\xffjumb\xff")
    checar(b"\xffc2pa\xffjumb\xff" in png_trns and b"tRNS" in png_trns, "n: o PNG de teste tem tRNS com os bytes c2pa e jumb")
    # PNG com perfil iCCP cujo conteúdo binário contém c2pa e jumb (zlib sem compressão, para os bytes aparecerem crus)
    carga_icc = perfil_icc() + b"\0c2pa\0jumb\0"
    png_icc = inserir_chunk_png(png_puro(imagem(var=21)), b"iCCP", b"perfil\0\0" + zlib.compress(carga_icc, 0))
    checar(b"\0c2pa\0jumb\0" in png_icc, "n: o PNG de teste tem iCCP com os bytes c2pa e jumb crus")
    # JPG com ICC (APP2) cujo conteúdo contém Veo, c2pa e jumb
    jpg_icc = jpeg_com_icc_binario(b"\0Veo\0c2pa\0jumb\0")
    checar(b"\0Veo\0c2pa\0jumb\0" in jpg_icc, "n: o JPG de teste tem APP2 ICC com os bytes Veo, c2pa e jumb")
    itens = [("png-paleta-veo.png", png_veo), ("png-trns.png", png_trns), ("png-iccp.png", png_icc), ("jpg-icc.jpg", jpg_icc)]
    if FFMPEG:
        try:
            itens.append(("mp4-tabela.mp4", mp4_com_bytes_na_tabela(pasta)))
        except Exception as e:
            checar(False, "n: montar o MP4 com bytes na tabela stts", repr(e))
    else:
        PULADOS.append("n (mp4)")
    caminhos = []
    for nome, dados in itens:
        orig = gravar_original(pasta / nome, dados)
        caminhos.append(orig)
        rot = f"n {nome}"
        rc, txt = rodar("analisar", orig)
        checar(rc == 0 and "[ok]" in txt and "nenhuma marca" in txt and "[!]" not in txt, f"{rot}: analisar não dá alarme falso", txt[:250])
        rc, txt = rodar("limpar", orig, "--saida", saida / nome)
        checar(rc == 0 and "[ok] arquivo limpo" in txt, f"{rot}: limpar termina em [ok] arquivo limpo", txt[-250:])
        checar("antes : 0 marca(s)" in txt and "depois: 0 marca(s)" in txt, f"{rot}: o boletim mostra 0 marca antes e 0 depois", txt[-250:])
        checar(ultima_linha_util(txt) == "Resumo: 1 de 1 arquivo(s) limpo(s)", f"{rot}: conta como limpo no resumo", ultima_linha_util(txt))
        d = saida / nome / (Path(nome).stem + "_limpo" + Path(nome).suffix)
        if not checar(d.is_file(), f"{rot}: saída criada"):
            continue
        if nome.endswith(".mp4"):
            conferir_video_abre(rot, str(d))
            conferir_video_igual(rot, orig, str(d))
            corpo = []
            mp4_trocar_caixa(dados, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stts"], lambda c: corpo.append(c) or c)
            checar(corpo and all(c in corpo[0] for c in (b"Veo", b"c2pa", b"jumb")), f"{rot}: o original tem mesmo as três cadeias dentro da caixa stts")
        else:
            conferir_pixels_iguais(rot, dados, d)
    # os mesmos arquivos num lote só
    rc, txt = rodar("limpar", *caminhos, "--saida", saida / "lote")
    n = len(caminhos)
    checar(rc == 0 and ultima_linha_util(txt) == f"Resumo: {n} de {n} arquivo(s) limpo(s)", f"n: lote dos {n} arquivos termina com resumo {n} de {n}", f"rc={rc} {ultima_linha_util(txt)!r}")
    if not FFMPEG:
        print("[AVISO] n: caso do MP4 com bytes em tabela de amostras ficou sem teste (sem ffmpeg)")


# ------------------------------------------------- (o) a conferência não ficou cega
def cenario_o(T):
    print("\n--- (o) a conferência não ficou cega")
    pasta = T / "o"
    rotulo = {b"Veo": "Google Veo", b"c2pa": "manifesto C2PA", b"jumb": "contêiner JUMBF"}
    # 1) linha de comando: analisar acusa a marca em região de TEXTO de um arquivo de entrada
    for marca, nome_marca in rotulo.items():
        txtm = b" nota " + marca + b" fim "
        base = jpeg_puro()
        pos = 4 + struct.unpack(">H", base[4:6])[0]
        casos = [
            ("jpg-com", base[:pos] + seg(0xFE, b"comentario" + txtm) + base[pos:]),
            ("png-text", inserir_chunk_png(png_puro(), b"tEXt", b"Comment\0" + txtm)),
            ("mp4-udta", mp4_minimo(moov_extra=caixa(b"udta", caixa(b"\xa9cmt", b"\0\x08\0\0" + txtm)))),
            ("mp4-uuid", mp4_minimo(topo_extra=caixa(b"uuid", bytes(range(16)) + txtm))),
        ]
        for tag, dados in casos:
            ext = {"jpg": ".jpg", "png": ".png", "mp4": ".mp4"}[tag[:3]]
            orig = gravar_original(pasta / f"{tag}-{marca.decode()}{ext}", dados)
            rc, txt = rodar("analisar", orig)
            checar(rc == 0 and "[!]" in txt and nome_marca in txt, f"o analisar {tag} com '{marca.decode()}' em texto: acusa {nome_marca}", txt[:250])
    # a limpeza de JPG e PNG com a marca em texto vê a marca antes e termina limpa
    for tag in ("jpg-com", "png-text"):
        ext = ".jpg" if tag.startswith("jpg") else ".png"
        orig = pasta / f"{tag}-c2pa{ext}"
        rc, txt = rodar("limpar", orig, "--saida", pasta / "saida")
        checar(rc == 0 and "[ok] arquivo limpo" in txt and "antes : 0 marca(s)" not in txt, f"o limpar {tag}: enxerga a marca antes e termina limpo", txt[-250:])
    # 1b) perfil de cor com texto dentro (comportamento documentado): o perfil ICC é dado de cor, a limpeza o mantém INTEIRO e a
    # conferência final não procura palavras nele. Então um JPG com texto dentro do APP2 ICC e um PNG com texto no NOME do iCCP
    # saem [ok], com o perfil de cor da saída idêntico byte a byte ao do original.
    def icc_jpg(dados):
        return [c for m, c in marcadores_jpeg(dados)[0] if m == 0xE2 and c.startswith(b"ICC_PROFILE")]

    def iccp_png(dados):
        return [bruto for t, bruto in chunks_png(dados)[0] if t == b"iCCP"]

    base = jpeg_puro()
    pos = 4 + struct.unpack(">H", base[4:6])[0]
    icc_jpg_texto = seg(0xE2, b"ICC_PROFILE" + bytes([0, 1, 1]) + perfil_icc() + b" gerado com Midjourney TEIMOSO-INJETADO")
    icc_png_texto = chunk(b"iCCP", b"Midjourney TEIMOSO-INJETADO\0\0" + zlib.compress(perfil_icc()))
    teimosos = [("jpg-icc-com-texto.jpg", base[:pos] + icc_jpg_texto + base[pos:], icc_jpg),
                ("png-nome-iccp.png", inserir_chunk_png(png_puro(), b"iCCP", icc_png_texto[8:-4]), iccp_png)]
    for nome, dados, ler_perfil in teimosos:
        orig = gravar_original(pasta / nome, dados)
        saida_t = pasta / "teimoso" / nome
        rc, txt = rodar("limpar", orig, "--saida", saida_t)
        d = saida_t / (Path(nome).stem + "_limpo" + Path(nome).suffix)
        rot = f"o perfil de cor com texto ({nome})"
        checar(rc == 0 and "[ok] arquivo limpo" in txt and "depois: 0 marca(s)" in txt and "[x]" not in txt,
               f"{rot}: limpar termina em [ok], com 0 marca depois", f"rc={rc} {txt[-250:]}")
        checar(ultima_linha_util(txt) == "Resumo: 1 de 1 arquivo(s) limpo(s)", f"{rot}: conta como limpo no resumo", ultima_linha_util(txt))
        if not checar(d.is_file(), f"{rot}: a cópia ficou no disco"):
            continue
        out = d.read_bytes()
        po, ps = ler_perfil(dados), ler_perfil(out)
        checar(len(po) == 1 and po == ps, f"{rot}: perfil de cor da saída idêntico byte a byte ao do original",
               f"original={len(po)} bloco(s), saída={len(ps)} bloco(s), iguais={po == ps}")
        checar(b"TEIMOSO-INJETADO" in ps[0] and b"Midjourney" in ps[0] if ps else False, f"{rot}: o texto continua dentro do perfil (mantido inteiro)")
        conferir_pixels_iguais(rot, dados, d)
    # ... e a MESMA palavra num bloco de metadado de verdade (comentário JPG, tEXt do PNG) some, mesmo com o perfil de cor ao lado
    base = jpeg_puro()
    pos = 4 + struct.unpack(">H", base[4:6])[0]
    mistos = [("jpg-icc-e-comentario.jpg", base[:pos] + icc_jpg_texto + seg(0xFE, b"Midjourney COMENTARIO-TEIMOSO") + base[pos:], icc_jpg),
              ("png-iccp-e-text.png", inserir_chunk_png(inserir_chunk_png(png_puro(), b"iCCP", icc_png_texto[8:-4]), b"tEXt",
                                                       b"Comment\0Midjourney COMENTARIO-TEIMOSO"), iccp_png)]
    for nome, dados, ler_perfil in mistos:
        orig = gravar_original(pasta / nome, dados)
        saida_t = pasta / "teimoso" / nome
        rc, txt = rodar("limpar", orig, "--saida", saida_t)
        d = saida_t / (Path(nome).stem + "_limpo" + Path(nome).suffix)
        rot = f"o: palavra no perfil e no metadado ({nome})"
        checar(rc == 0 and "[ok] arquivo limpo" in txt, f"{rot}: limpar termina em [ok]", f"rc={rc} {txt[-250:]}")
        if not checar(d.is_file(), f"{rot}: a cópia ficou no disco"):
            continue
        out = d.read_bytes()
        checar(b"COMENTARIO-TEIMOSO" not in out, f"{rot}: o comentário/tEXt com a palavra saiu")
        checar(out.count(b"Midjourney") == 1 and ler_perfil(out) == ler_perfil(dados),
               f"{rot}: a palavra só resta dentro do perfil de cor, que é idêntico ao do original", f"ocorrências={out.count(b'Midjourney')}")
    # 2) função de conferência de saída chamada direto (importa o módulo: exceção marcada em carregar_modulo)
    try:
        mod = carregar_modulo()
        achar = mod.achar_marcas
    except Exception as e:
        checar(False, "o: importar achar_marcas do script", repr(e))
        return
    base = jpeg_puro()
    pos = 4 + struct.unpack(">H", base[4:6])[0]
    tiff, _ = montar_exif(1, True)
    sujo_jpg = base[:pos] + seg(0xE1, b"Exif\0\0" + tiff) + seg(0xFE, b"sem palavra de IA, so um comentario") + base[pos:]
    sujo_png = png_com_tudo(png_puro())[0]
    sujo_mp4 = mp4_minimo(topo_extra=caixa(b"uuid", C2PA_UUID + b"jumb c2pa"))
    for nome, dados, tipo in (("JPG com APP1 e COM", sujo_jpg, "jpeg"), ("PNG com tEXt e bytes depois do IEND", sujo_png, "png"),
                              ("MP4 com caixa uuid do C2PA", sujo_mp4, "mp4")):
        try:
            r = achar(dados, tipo, saida=True)
        except Exception as e:
            r = repr(e)
        checar(isinstance(r, list) and len(r) >= 1, f"o achar_marcas(saida=True) devolve marca para {nome}", f"devolveu {r!r}")
    # cada marca curta sozinha, em texto, também precisa ser vista na conferência de saída
    for marca in rotulo:
        txtm = b" nota " + marca + b" fim "
        for nome, dados, tipo in (("JPG COM", base[:pos] + seg(0xFE, txtm) + base[pos:], "jpeg"),
                                  ("PNG tEXt", inserir_chunk_png(png_puro(), b"tEXt", b"Comment\0" + txtm), "png"),
                                  ("PNG depois do IEND", png_puro() + txtm, "png"),
                                  ("MP4 uuid", mp4_minimo(topo_extra=caixa(b"uuid", bytes(range(16)) + txtm)), "mp4")):
            r = achar(dados, tipo, saida=True)
            checar(len(r) >= 1, f"o achar_marcas(saida=True): '{marca.decode()}' em {nome}", f"devolveu {r!r}")
    # contraprova: arquivos limpos não geram alarme
    for nome, dados, tipo in (("JPG limpo", jpeg_puro(), "jpeg"), ("PNG limpo", png_puro(), "png"), ("MP4 limpo", mp4_minimo(), "mp4"),
                              ("PNG paleta Veo", png_paleta([(0, 0, 0), (0x56, 0x65, 0x6F), (0, 0, 0)])[0], "png")):
        r = achar(dados, tipo, saida=True)
        checar(r == [], f"o achar_marcas(saida=True): {nome} não gera alarme", f"devolveu {r!r}")


# ------------------------------------------------ (p) orientação EXIF inválida
def cenario_p(T):
    print("\n--- (p) orientação EXIF 0 e 9 não recodifica; 2 a 8 continuam girando")
    saida = T / "p" / "saida"
    for o in (0, 9):
        dados, cadeias = jpeg_com_tudo(orient=o)
        orig = gravar_original(T / "p" / f"orient{o}.jpg", dados)
        rot = f"p orientação {o}"
        rc, txt = rodar("limpar", orig, "--saida", saida)
        checar(rc == 0 and "[ok] arquivo limpo" in txt, f"{rot}: limpar termina em [ok]", txt[-250:])
        checar("método: sem recodificar" in txt and "recodificado" not in txt, f"{rot}: o método impresso diz 'sem recodificar'", txt[:250])
        d = saida / f"orient{o}_limpo.jpg"
        if not checar(d.is_file(), f"{rot}: saída criada"):
            continue
        out = d.read_bytes()
        checar(imagem_comprimida(out) == imagem_comprimida(dados), f"{rot}: dados comprimidos da imagem idênticos aos do original (tabelas, quadro e varredura)")
        conferir_jpeg_limpo(rot, out, cadeias)
        conferir_pixels_iguais(rot, dados, d)
    for o in range(2, 9):
        dados, _ = jpeg_com_tudo(orient=o, little=(o % 2 == 0))
        orig = gravar_original(T / "p" / f"gira{o}.jpg", dados)
        rc, txt = rodar("limpar", orig, "--saida", saida)
        d = saida / f"gira{o}_limpo.jpg"
        checar(rc == 0 and "método: recodificado" in txt, f"p orientação {o}: continua girando (método recodificado)", txt[:250])
        if d.is_file():
            w0, h0 = pixels(dados)[1]
            w1, h1 = pixels(d.read_bytes())[1]
            esperado = (h0, w0) if o >= 5 else (w0, h0)
            checar((w1, h1) == esperado, f"p orientação {o}: dimensões {(w1, h1)} conforme a rotação", f"esperado {esperado}")


# ----------------------------------------------------------- (q) pasta como argumento
def cenario_q(T):
    print("\n--- (q) uma pasta no meio dos argumentos")
    pasta = T / "q"
    saida = pasta / "saida"
    (pasta / "uma-pasta").mkdir(parents=True)
    interno = gravar_original(pasta / "uma-pasta" / "dentro.jpg", jpeg_com_tudo()[0])
    (pasta / "pasta-com-cara-de-foto.jpg").mkdir()
    j = gravar_original(pasta / "bom.jpg", jpeg_com_tudo()[0])
    p = gravar_original(pasta / "bom.png", png_com_tudo(png_puro())[0])
    rc, txt = rodar("limpar", str(pasta / "uma-pasta"), j, str(pasta / "pasta-com-cara-de-foto.jpg"), p, "--saida", saida)
    checar(rc != 0, "q: código de saída diferente de 0", f"rc={rc}")
    linhas = [l for l in txt.splitlines() if l.startswith("[x]")]
    checar(sum("é uma pasta" in l for l in linhas) == 2, "q: a mensagem diz que é uma pasta (para as duas)", f"linhas [x]: {linhas}")
    checar("não encontrado" not in txt, "q: pasta não é chamada de 'não encontrada'", txt[:300])
    resumos = [l.strip() for l in txt.splitlines() if l.strip().startswith("Resumo:")]
    checar(resumos == ["Resumo: 2 de 4 arquivo(s) limpo(s)"], "q: a pasta conta como não limpa (resumo 2 de 4)", f"{resumos}")
    for nome in ("uma-pasta", "pasta-com-cara-de-foto.jpg"):
        checar(any(l.strip().startswith("não limpo:") and nome in l for l in txt.splitlines()), f"q: {nome} aparece como não limpo", txt[-300:])
    for nome in ("bom_limpo.jpg", "bom_limpo.png"):
        d = saida / nome
        if checar(d.is_file(), f"q: {nome} foi limpo apesar da pasta no pedido"):
            out = d.read_bytes()
            checar(b"c2pa" not in out and b"Midjourney" not in out, f"q: {nome} sem marcas")
    extras = sorted(x.name for x in saida.iterdir()) if saida.is_dir() else []
    checar(extras == ["bom_limpo.jpg", "bom_limpo.png"], "q: nada foi gravado para as pastas", f"saída tem {extras}")
    checar(sorted(x.name for x in (pasta / "uma-pasta").iterdir()) == ["dentro.jpg"], "q: o conteúdo da pasta passada não foi tocado nem ganhou 'limpos'")
    checar(sha(interno) == HASHES[interno], "q: o arquivo dentro da pasta continua igual")
    rc, txt = rodar("analisar", str(pasta / "uma-pasta"), j)
    checar(rc != 0 and "é uma pasta" in txt and "[!]" in txt, "q: analisar também diz que é pasta (código != 0) e segue para os outros", f"rc={rc} {txt[:250]}")


# ====================================================== casos novos (r a w)
def gerar_video_especial(destino, ext=".mp4", vf=None, escala=None, bf=None, marcado=False, duracao=12, preset="ultrafast", tmp=None):
    """Vídeo de teste com tempos sob controle. vf = expressão do setpts (em unidades de 1/escala), escala = base de tempo do
    vídeo (também a escala da trilha no MP4), bf = quadros B do libx264. marcado=True põe tags e caixas uuid de C2PA e XMP,
    como gerar_video. Devolve os bytes do arquivo pronto."""
    entrada = ["-y", "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate=30:duration={duracao}",
               "-f", "lavfi", "-i", f"sine=frequency=330:duration={duracao}", "-map", "0:v", "-map", "1:a"]
    cod = ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k"]
    if bf is not None:
        cod += ["-bf", str(bf)]
    if escala:
        cod += ["-video_track_timescale", str(escala)]
    if marcado:
        cod += ["-metadata", "title=TITULO-INJETADO", "-metadata", "comment=COMENTARIO-VIDEO-INJETADO c2pa",
                "-metadata:s:v:0", "handler_name=HANDLER-INJETADO"]
    # -fps_mode (ffmpeg 5.1 ou mais novo); o -vsync é o nome antigo da mesma opção
    for pt in (["-fps_mode", "passthrough"], ["-vsync", "passthrough"]):
        filtro = ["-vf", f"settb=1/{escala},setpts='{vf}'", *pt, "-enc_time_base", f"1/{escala}"] if vf else []
        rc, _, err = ff(*entrada, *filtro, *cod, destino)
        if rc == 0 or not vf:
            break
    if rc != 0:
        raise RuntimeError("ffmpeg não gerou o vídeo de teste: " + err[:200])
    dados = Path(destino).read_bytes()
    Path(destino).unlink()
    if marcado:
        dados += caixa(b"uuid", C2PA_UUID + b"jumb c2pa C2PA-UUID-TEXTO-INJETADO Google Veo")
        dados += caixa(b"uuid", XMP_UUID + b"<x:xmpmeta DigitalSourceType trainedAlgorithmicMedia XMP-UUID-TEXTO-INJETADO/>")
    return dados


# três jeitos de taxa variável. As expressões dão o tempo de apresentação de cada quadro N, em unidades de 1/escala.
VFR_CASOS = [
    # nome, escala, expressão, o que confere
    ("30,05 qps com tremor", 90000, "round(N*90000/30.05)+floor(random(0)*19)-9", "tremor"),
    ("cadência alternando 24 e 30 qps", 90000,
     "(floor((floor(N/30)+1)/2)*30/24+floor(floor(N/30)/2)*30/30+(N-30*floor(N/30))/if(eq(mod(floor(N/30),2),0),24,30))*90000", "alterna"),
    ("tempos em milissegundo irregulares", 1000, "round(N*1000/30)+floor(random(0)*13)-6", "ms"),
]


def intervalos_de_video(caminho):
    """Conjunto dos intervalos distintos entre quadros consecutivos (pts em segundos exatos, ordenados) e a base de tempo."""
    f = pacotes_por_fluxo(caminho)
    v = f.get(("video", 0)) if f else None
    if not v:
        return None, None
    t = sorted(p[0] for p in v["pacotes"] if p[0] is not None)
    return {b - a for a, b in zip(t, t[1:])}, v["base"]


def cenario_r(T):
    print("\n--- (r) vídeo de taxa de quadros variável, sem marca e com marca de IA")
    if not FFMPEG or not FFPROBE:
        PULADOS.append("r")
        print("[AVISO] ffmpeg/ffprobe ausentes: testes de taxa variável PULADOS")
        return
    saida = T / "r" / "saida"
    for nome, escala, expr, tipo in VFR_CASOS:
        for marcado in (False, True):
            tag = {"tremor": "tremor", "alterna": "alterna", "ms": "ms"}[tipo] + ("-marca" if marcado else "-limpo")
            rot = f"r {nome} {'com marca' if marcado else 'sem marca'}"
            tmp = T / "r" / f"gerado-{tag}.mp4"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            try:
                dados = gerar_video_especial(str(tmp), vf=expr, escala=escala, bf=0, marcado=marcado)
            except Exception as e:
                checar(False, f"{rot}: gerar vídeo de teste", str(e))
                continue
            orig = gravar_original(T / "r" / f"vfr-{tag}.mp4", dados)
            # o original é mesmo de taxa variável, na escala pedida
            iv, base = intervalos_de_video(orig)
            checar(iv is not None and len(iv) >= 3 if tipo != "alterna" else iv is not None and {Fraction(1, 24), Fraction(1, 30)} <= iv,
                   f"{rot}: o original tem intervalos diferentes entre os quadros (taxa variável)", f"{len(iv or [])} intervalos distintos: {sorted(iv or [])[:4]}")
            checar(base == Fraction(1, escala), f"{rot}: a base de tempo do vídeo é 1/{escala}", f"base={base}")
            if marcado:
                faltam = faltando_em(dados, CADEIAS_VIDEO)
                # Lavf e o texto de capítulo não vêm neste vídeo; confere só o que se injetou aqui
                faltam = [c for c in faltam if c not in (b"CAPITULO-INJETADO", b"CAPITULO2-INJETADO", b"ENCODER-INJETADO")]
                checar(not faltam, f"{rot}: o original contém as cadeias injetadas", "faltam " + mostrar(faltam))
                rc, txt = rodar("analisar", orig)
                checar(rc == 0 and "[!]" in txt and "caixa uuid do C2PA" in txt, f"{rot}: analisar acusa a marca", txt[:200])
            rc, txt = rodar("limpar", orig, "--saida", saida)
            checar(rc == 0, f"{rot}: limpar termina com código 0", txt[-300:])
            checar("[ok] arquivo limpo" in txt and "[x]" not in txt, f"{rot}: termina em [ok] arquivo limpo", txt[-300:])
            checar(ultima_linha_util(txt) == "Resumo: 1 de 1 arquivo(s) limpo(s)", f"{rot}: resumo 1 de 1", ultima_linha_util(txt))
            d = saida / f"vfr-{tag}_limpo.mp4"
            if not checar(d.is_file(), f"{rot}: saída criada"):
                continue
            cadeias = [c for c in CADEIAS_VIDEO if c not in (b"CAPITULO-INJETADO", b"CAPITULO2-INJETADO", b"ENCODER-INJETADO")] if marcado else [b"Lavf"]
            conferir_video_limpo(rot, dados, str(d), cadeias, passthrough=True, original=orig)
            conferir_video_igual(rot, orig, str(d), passthrough=True)
            pv = (pacotes_por_fluxo(d) or {}).get(("video", 0))
            dts = [p[1] for p in pv["pacotes"]] if pv else []
            checar(len(dts) > 100 and all(x is not None for x in dts) and all(b > a for a, b in zip(dts, dts[1:])),
                   f"{rot}: no arquivo limpo o tempo de decodificação do vídeo cresce sempre (o arquivo é válido)", f"{len(dts)} pacotes")
            iv2, base2 = intervalos_de_video(d)
            checar(iv2 == iv, f"{rot}: os intervalos entre quadros do limpo são os do original", f"original={sorted(iv or [])[:3]} limpo={sorted(iv2 or [])[:3]}")


# ------------------------------------------------- (s) original protegido por atalho
def _criar_juncao(atalho, alvo):
    """Junção (mklink /J) no Windows; link simbólico em outro sistema. Devolve None se criou, ou o motivo de não ter criado."""
    try:
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(atalho), str(alvo)], capture_output=True)
            if r.returncode != 0 or not Path(atalho).exists():
                return (r.stdout + r.stderr).decode("cp850", errors="replace").strip() or "mklink falhou"
        else:
            os.symlink(str(alvo), str(atalho), target_is_directory=True)
    except Exception as e:
        return repr(e)
    return None


def _nome_curto(caminho):
    """Nome curto 8.3 de um caminho que existe (Windows). None se o sistema ou o disco não tem."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(caminho), buf, 1024)
        return buf.value if n and buf.value and buf.value.lower() != str(caminho).lower() else None
    except Exception:
        return None


def _conferir_apelido(rot, ext, real, pedido_1, pedido_2, saida_arg, orig_1, orig_2):
    """Roda o pedido com a pasta escrita de dois jeitos e confere: os dois originais mantêm o sha256, saem duas cópias distintas,
    cada uma com os pixels do seu original."""
    pa, pb = pixels(Path(orig_1).read_bytes())[2], pixels(Path(orig_2).read_bytes())[2]
    checar(pa != pb, f"{rot}: os dois originais são mesmo diferentes")
    antes = {p.name for p in Path(real).iterdir()}
    rc, txt = rodar("limpar", pedido_1, pedido_2, "--saida", saida_arg)
    checar(rc == 0, f"{rot}: limpar termina com código 0", txt[-300:])
    checar(ultima_linha_util(txt) == "Resumo: 2 de 2 arquivo(s) limpo(s)", f"{rot}: o resumo diz 2 de 2", ultima_linha_util(txt))
    for o in (orig_1, orig_2):
        checar(Path(o).is_file() and sha(o) == HASHES[o], f"{rot}: {Path(o).name} manteve o sha256")
    novos = sorted(p for p in Path(real).iterdir() if p.is_file() and p.name not in antes)
    checar(len(novos) == 2, f"{rot}: saem duas cópias novas, além dos dois originais", f"novos: {[p.name for p in novos]} (já existiam {sorted(antes)})")
    if len(novos) == 2:
        pix = [pixels(p.read_bytes())[2] for p in novos]
        checar(sorted(pix) == sorted([pa, pb]), f"{rot}: cada cópia tem os pixels de um original diferente")
        checar(len({p.read_bytes() for p in novos}) == 2, f"{rot}: as duas cópias são arquivos distintos")
        for p in novos:
            out = p.read_bytes()
            checar(b"c2pa" not in out and b"Midjourney" not in out and b"trainedAlgorithmicMedia" not in out, f"{rot}: {p.name} sem marcas")


def cenario_s(T):
    print("\n--- (s) original protegido com a mesma pasta escrita de dois jeitos (junção e nome curto 8.3)")
    fabricas = ((".jpg", lambda v: jpeg_com_tudo(imagem(var=v))[0]), (".png", lambda v: png_com_tudo(png_puro(imagem(var=v)))[0]))
    # s1: junção. apelido -> real; o original 1 é pedido por "apelido/foto", o 2 por "real/foto_limpo", e a saída é "apelido"
    for ext, fazer in fabricas:
        for ordem in ("apelido primeiro", "real primeiro"):
            base = T / "s1" / ext[1:] / ordem.split()[0]
            real, apelido = base / "real", base / "apelido"
            real.mkdir(parents=True)
            erro = _criar_juncao(apelido, real)
            rot = f"s junção {ext} ({ordem})"
            if erro:
                PULADOS.append(f"s junção {ext}")
                print(f"[AVISO] {rot}: não consegui criar a junção neste sistema ou disco ({erro[:120]}); caso PULADO")
                continue
            try:
                a = gravar_original(real / f"foto{ext}", fazer(25))
                b = gravar_original(real / f"foto_limpo{ext}", fazer(165))
                if ordem == "apelido primeiro":
                    _conferir_apelido(rot, ext, real, str(apelido / f"foto{ext}"), str(real / f"foto_limpo{ext}"), str(apelido), a, b)
                else:
                    _conferir_apelido(rot, ext, real, str(real / f"foto{ext}"), str(apelido / f"foto_limpo{ext}"), str(real), a, b)
            finally:
                try:
                    os.rmdir(apelido)  # remove só o atalho, nunca o conteúdo
                except OSError:
                    pass
    # s2: nome curto 8.3 da pasta
    for ext, fazer in fabricas:
        pasta = T / "s2" / ext[1:] / "pasta com nome bem longo"
        pasta.mkdir(parents=True)
        a = gravar_original(pasta / f"foto{ext}", fazer(35))
        b = gravar_original(pasta / f"foto_limpo{ext}", fazer(155))
        curto = _nome_curto(pasta)
        rot = f"s nome curto 8.3 {ext}"
        if not curto:
            PULADOS.append(f"s nome curto 8.3 {ext}")
            print(f"[AVISO] {rot}: este sistema ou disco não tem nome curto 8.3 (ou não é Windows); caso PULADO")
            continue
        checar(Path(curto).is_dir() and os.path.samefile(curto, pasta) and curto != str(pasta), f"{rot}: o nome curto aponta para a mesma pasta ({Path(curto).name})")
        _conferir_apelido(rot, ext, pasta, str(Path(curto) / f"foto{ext}"), str(pasta / f"foto_limpo{ext}"), curto, a, b)


# ------------------------------------------------------------ (t) foto muito grande
def dimensoes_jpeg(dados):
    """(largura, altura) lidas do quadro (SOF) sem decodificar a imagem."""
    for m, c in marcadores_jpeg(dados)[0]:
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return struct.unpack(">H", c[3:5])[0], struct.unpack(">H", c[1:3])[0]
    return None


def cenario_t(T):
    print("\n--- (t) foto muito grande: JPG de 14000 x 13000 pixels (182 megapixels, acima do dobro do limite padrão do Pillow)")
    larg, alt = 14000, 13000
    pasta = T / "t"
    try:
        # tons de cinza: 1 byte por pixel. O degradê é esticado para o tamanho final e leva um risco para não ser liso.
        im = Image.linear_gradient("L").resize((larg, alt))
        im.paste(255, (0, 0, 2000, 300))
        dados, cadeias = jpeg_com_tudo(im)
        dados_g, cadeias_g = jpeg_com_tudo(im, orient=6)  # a mesma foto com rotação EXIF: este caminho abre e regrava a imagem
        del im
    except MemoryError:
        PULADOS.append("t")
        print("[AVISO] t: a máquina não tem memória para montar a foto de 14000 x 13000; caso PULADO")
        return
    orig = gravar_original(pasta / "enorme.jpg", dados)
    checar(dimensoes_jpeg(dados) == (larg, alt) and larg * alt > 2 * 89478485, "t: o JPG de teste tem 14000 x 13000 e passa do dobro do limite do Pillow", str(dimensoes_jpeg(dados)))
    saida = pasta / "saida"
    rc, txt = rodar("limpar", orig, "--saida", saida)
    if rc != 0 and ("MemoryError" in txt or "emória" in txt or "Unable to allocate" in txt):
        PULADOS.append("t")
        print("[AVISO] t: a máquina ficou sem memória para a foto de 182 megapixels; caso PULADO. Saída: " + txt[-200:].replace("\n", " "))
        return
    checar(rc == 0, "t: limpar termina com código 0", txt[-300:])
    checar("[ok] arquivo limpo" in txt and "[x]" not in txt, "t: termina em [ok] arquivo limpo", txt[-300:])
    checar(ultima_linha_util(txt) == "Resumo: 1 de 1 arquivo(s) limpo(s)", "t: resumo 1 de 1", ultima_linha_util(txt))
    d = saida / "enorme_limpo.jpg"
    if not checar(d.is_file(), "t: saída criada"):
        return
    out = d.read_bytes()
    conferir_jpeg_limpo("t", out, cadeias)
    checar(dimensoes_jpeg(out) == (larg, alt), "t: a saída continua com 14000 x 13000", str(dimensoes_jpeg(out)))
    checar(imagem_comprimida(out) == imagem_comprimida(dados), "t: dados comprimidos da imagem idênticos aos do original (sem recodificar)")
    del out
    # t2: a foto enorme com rotação EXIF (orientação 6): o Pillow abre, gira e regrava
    orig_g = gravar_original(pasta / "enorme-girada.jpg", dados_g)
    rc, txt = rodar("limpar", orig_g, "--saida", saida)
    if rc != 0 and ("MemoryError" in txt or "emória" in txt or "Unable to allocate" in txt):
        PULADOS.append("t (foto girada)")
        print("[AVISO] t2: a máquina ficou sem memória para girar a foto de 182 megapixels; caso PULADO. Saída: " + txt[-200:].replace(chr(10), " "))
        return
    checar(rc == 0 and "[ok] arquivo limpo" in txt and "[x]" not in txt, "t2 girada: limpar termina em [ok] arquivo limpo", txt[-300:])
    checar("método: recodificado" in txt, "t2 girada: o método impresso é 'recodificado' (a rotação foi aplicada)", txt[:300])
    d = saida / "enorme-girada_limpo.jpg"
    if checar(d.is_file(), "t2 girada: saída criada"):
        out = d.read_bytes()
        conferir_jpeg_limpo("t2 girada", out, cadeias_g)
        checar(dimensoes_jpeg(out) == (alt, larg), "t2 girada: a saída tem 13000 x 14000 (largura e altura trocadas pela rotação)", str(dimensoes_jpeg(out)))


# --------------------------------------------------------------- (u) quadros B
def cenario_u(T):
    print("\n--- (u) vídeo com quadros B (libx264 com -bf 3) em MP4 e em MOV")
    if not FFMPEG or not FFPROBE:
        PULADOS.append("u")
        print("[AVISO] ffmpeg/ffprobe ausentes: testes de quadros B PULADOS")
        return
    saida = T / "u" / "saida"
    for ext in (".mp4", ".mov"):
        for marcado in (False, True):
            rot = f"u {ext[1:].upper()} com quadros B {'com marca' if marcado else 'sem marca'}"
            tag = "marca" if marcado else "limpo"
            tmp = T / "u" / f"gerado-{tag}{ext}"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            try:
                dados = gerar_video_especial(str(tmp), ext=ext, bf=3, marcado=marcado, duracao=6, preset="veryfast")
            except Exception as e:
                checar(False, f"{rot}: gerar vídeo de teste", str(e))
                continue
            orig = gravar_original(T / "u" / f"b-{tag}{ext}", dados)
            pk = pacotes_por_fluxo(orig)
            v = (pk or {}).get(("video", 0))
            com_b = [p for p in (v["pacotes"] if v else []) if p[0] is not None and p[1] is not None and p[0] != p[1]]
            checar(len(com_b) > 10, f"{rot}: o original tem quadros B (pts diferente de dts em vários pacotes)", f"{len(com_b)} pacote(s) com pts != dts")
            rc, txt = rodar("limpar", orig, "--saida", saida)
            checar(rc == 0 and "[ok] arquivo limpo" in txt and "[x]" not in txt, f"{rot}: limpar termina em [ok] arquivo limpo", txt[-300:])
            checar(ultima_linha_util(txt) == "Resumo: 1 de 1 arquivo(s) limpo(s)", f"{rot}: resumo 1 de 1", ultima_linha_util(txt))
            d = saida / f"b-{tag}_limpo{ext}"
            if not checar(d.is_file(), f"{rot}: saída criada"):
                continue
            cadeias = [c for c in CADEIAS_VIDEO if c not in (b"CAPITULO-INJETADO", b"CAPITULO2-INJETADO", b"ENCODER-INJETADO")] if marcado else [b"Lavf"]
            conferir_video_limpo(rot, dados, str(d), cadeias)
            conferir_video_igual(rot, orig, str(d))


# ----------------------------------------- (v) leitura sem carregar o mdat (importa o módulo)
def _mdat(carga, grande=False, tam_zero=False):
    if grande:  # cabeçalho de 64 bits: tamanho 1, tipo, e o tamanho real em 8 bytes
        return struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + len(carga)) + carga
    if tam_zero:  # tamanho 0: vai até o fim do arquivo
        return struct.pack(">I", 0) + b"mdat" + carga
    return caixa(b"mdat", carga)


def cenario_v(T):
    print("\n--- (v) _ler_para_conferir pula o mdat sem carregá-lo (IMPORTA O MÓDULO: exceção marcada à regra de independência)")
    pasta = T / "v"
    pasta.mkdir(parents=True)
    try:
        mod = carregar_modulo()  # EXCEÇÃO MARCADA (v)
        ler = mod._ler_para_conferir
    except Exception as e:
        checar(False, "v: importar _ler_para_conferir do script", repr(e))
        return
    ftyp = caixa(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2")
    texto_mdat = b"xx Midjourney MDAT-TEXTO-INJETADO c2pa jumb xx"
    texto_free = b"FREE-TEXTO-INJETADO"
    montagens = [
        ("mdat de 32 bits, free depois", ftyp + _mdat(texto_mdat) + caixa(b"free", texto_free)),
        ("mdat de cabeçalho de 64 bits, free depois", ftyp + _mdat(texto_mdat, grande=True) + caixa(b"free", texto_free)),
        ("free antes e mdat de 64 bits no meio, outro free depois", ftyp + caixa(b"free", texto_free) + _mdat(texto_mdat, grande=True) + caixa(b"free", b"SEGUNDO-FREE-INJETADO")),
        ("mdat de tamanho 0 (até o fim do arquivo)", ftyp + caixa(b"free", texto_free) + _mdat(texto_mdat, tam_zero=True)),
    ]
    for nome, dados in montagens:
        caminho = pasta / (nome.split(",")[0].replace(" ", "-") + ".mp4")
        caminho.write_bytes(dados)
        try:
            lido = ler(str(caminho), "mp4")
        except Exception as e:
            checar(False, f"v {nome}: a função lê o arquivo", repr(e))
            continue
        checar(texto_free in lido, f"v {nome}: o resultado traz o texto da caixa free", f"trouxe {len(lido)} byte(s)")
        checar(b"MDAT-TEXTO-INJETADO" not in lido and b"Midjourney" not in lido, f"v {nome}: o resultado NÃO traz o texto de dentro do mdat")
        checar(b"ftyp" in lido, f"v {nome}: o resultado traz a caixa ftyp")
        if "segundo" in nome or "outro free" in nome:
            checar(b"SEGUNDO-FREE-INJETADO" in lido, f"v {nome}: lê também a caixa que vem depois do mdat")
    # arquivo que não é MP4 continua lido inteiro
    jpg = pasta / "a.jpg"
    jpg.write_bytes(jpeg_puro())
    checar(ler(str(jpg), "jpeg") == jpg.read_bytes(), "v: JPG continua sendo lido inteiro")
    # de fato sem carregar: mdat de 160 MB e o pico de memória do Python na leitura fica muito abaixo disso
    import tracemalloc
    grande = pasta / "mdat-grande.mp4"
    with open(grande, "wb") as f:
        bloco = b"Midjourney MDAT-TEXTO-INJETADO " * 32768  # 1 MB de texto com palavras da lista
        vezes = 160
        f.write(ftyp + struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + vezes * len(bloco)))
        for _ in range(vezes):
            f.write(bloco)
        f.write(caixa(b"free", texto_free))
    tracemalloc.start()
    try:
        lido = ler(str(grande), "mp4")
        pico = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    checar(texto_free in lido and b"MDAT-TEXTO-INJETADO" not in lido and len(lido) < 1024, "v: mdat de 160 MB: lê só a caixa free e o ftyp", f"{len(lido)} byte(s)")
    checar(pico < 16 * 1024 * 1024, "v: mdat de 160 MB: o pico de memória da leitura é pequeno (menos de 16 MB)", f"pico {pico / 1048576:.1f} MB")
    grande.unlink()


# ------------------------------------------- (w) a conferência do vídeo recusa cópia trocada
def cenario_w(T):
    print("\n--- (w) validar_saida recusa cópia trocada (IMPORTA O MÓDULO: exceção marcada à regra de independência)")
    if not FFMPEG or not FFPROBE:
        PULADOS.append("w")
        print("[AVISO] ffmpeg/ffprobe ausentes: teste da conferência de vídeo PULADO")
        return
    pasta = T / "w"
    pasta.mkdir(parents=True)
    try:
        mod = carregar_modulo()  # EXCEÇÃO MARCADA (w)
        validar = mod.validar_saida
    except Exception as e:
        checar(False, "w: importar validar_saida do script", repr(e))
        return

    def video(nome, fonte, audio, tamanho="160x120"):
        destino = pasta / nome
        rc, _, err = ff("-y", "-f", "lavfi", "-i", f"{fonte}=size={tamanho}:rate=15:duration=2", "-f", "lavfi", "-i", f"sine=frequency={audio}:duration=2",
                        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", destino)
        if rc != 0:
            raise RuntimeError("ffmpeg não gerou o vídeo de teste: " + err[:200])
        return str(destino)

    try:
        original = video("original.mp4", "testsrc2", 440)
        outro_video = video("outro-video.mp4", "smptebars", 440)
        outro_audio = video("outro-audio.mp4", "testsrc2", 880)
        outro_tamanho = video("outro-tamanho.mp4", "testsrc2", 440, tamanho="192x144")
    except Exception as e:
        checar(False, "w: gerar os vídeos de teste", str(e))
        return
    registrar_original_existente(original)
    # cópia fiel: o próprio remonte do script, feito pela linha de comando
    rc, txt = rodar("limpar", original, "--saida", pasta / "fiel")
    fiel = pasta / "fiel" / "original_limpo.mp4"
    if not checar(rc == 0 and fiel.is_file(), "w: o remonte do script termina em [ok] e existe", txt[-200:]):
        return
    try:
        validar("mp4", str(fiel), original)
        levantou = None
    except Exception as e:
        levantou = repr(e)
    checar(levantou is None, "w: cópia fiel (o próprio remonte): validar_saida não levanta erro", str(levantou))
    for nome, copia, o_que in ((" com outro vídeo (conteúdo diferente, mesmos parâmetros)", outro_video, "o vídeo"),
                               (" com outro áudio (conteúdo diferente)", outro_audio, "o áudio"),
                               (" com outro tamanho de imagem", outro_tamanho, "o tamanho")):
        try:
            validar("mp4", copia, original)
            levantou, msg = False, ""
        except Exception as e:
            levantou, msg = True, str(e)
        checar(levantou, f"w: cópia trocada{nome}: validar_saida levanta erro", "aceitou uma cópia que é outro vídeo")
        if levantou:
            checar(len(msg) > 10, f"w: cópia trocada{nome}: o erro traz uma mensagem", repr(msg))
    # e a cópia fiel de um vídeo de taxa variável também passa
    tmp = pasta / "g-vfr.mp4"
    try:
        dados = gerar_video_especial(str(tmp), vf=VFR_CASOS[0][2], escala=VFR_CASOS[0][1], bf=0, marcado=False, duracao=6)
    except Exception as e:
        checar(False, "w: gerar vídeo de taxa variável", str(e))
        return
    vfr = gravar_original(pasta / "vfr.mp4", dados)
    rc, txt = rodar("limpar", vfr, "--saida", pasta / "fiel")
    fiel_v = pasta / "fiel" / "vfr_limpo.mp4"
    if checar(rc == 0 and fiel_v.is_file(), "w: o remonte do vídeo de taxa variável termina em [ok]", txt[-200:]):
        try:
            validar("mp4", str(fiel_v), vfr)
            levantou = None
        except Exception as e:
            levantou = repr(e)
        checar(levantou is None, "w: cópia fiel de vídeo de taxa variável: validar_saida não levanta erro", str(levantou))
        try:
            validar("mp4", str(fiel), vfr)
            levantou = False
        except Exception:
            levantou = True
        checar(levantou, "w: o remonte de OUTRO vídeo apresentado como cópia do de taxa variável: levanta erro")


def cenario_h():
    print("\n--- (h) os originais nunca mudam")
    for caminho, h in sorted(HASHES.items()):
        checar(Path(caminho).is_file() and sha(caminho) == h, f"h: original intacto ({Path(caminho).parent.name}/{Path(caminho).name})",
               "sha256 diferente ou arquivo sumiu")


# ----------------------------------------- (x) corte escondido pela lista de edição
def video_com_fim_escondido(destino, bf, seg=4):
    """Gera um vídeo e encurta pela metade o último trecho de cada lista de
    edição. É o corte sem recodificar: a segunda metade continua dentro do
    arquivo e só não é exibida."""
    rc, _, err = ff("-y", "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate=30:duration={seg}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seg}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-bf", str(bf), "-g", "15", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-use_editlist", "1", destino)
    if rc != 0:
        raise RuntimeError(err[-300:])
    d = bytearray(Path(destino).read_bytes())
    pos = d.find(b"mvhd")
    escala = struct.unpack(">I", d[pos + 16:pos + 20])[0] if d[pos + 4] == 0 else struct.unpack(">I", d[pos + 24:pos + 28])[0]
    i = 0
    while True:
        i = d.find(b"elst", i)
        if i < 0:
            break
        versao = d[i + 4]
        n = struct.unpack(">I", d[i + 8:i + 12])[0]
        ult = i + 12 + (n - 1) * (20 if versao == 1 else 12)
        nova = int(seg / 2 * escala)
        if versao == 1:
            d[ult:ult + 8] = struct.pack(">Q", nova)
        else:
            d[ult:ult + 4] = struct.pack(">I", nova)
        i += 4
    return bytes(d)


def duracoes_exibidas(caminho):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "csv=p=0", str(caminho)],
                       capture_output=True)
    out = {}
    for linha in r.stdout.decode("utf-8", errors="replace").splitlines():
        partes = linha.strip().split(",")
        if len(partes) >= 2:
            try:
                out[partes[0]] = float(partes[1])
            except ValueError:
                pass
    return out


def cenario_x(T):
    print("\n--- (x) vídeo com o fim escondido pela lista de edição (corte sem recodificar)")
    if not FFMPEG or not FFPROBE:
        PULADOS.append("x")
        print("[AVISO] ffmpeg/ffprobe ausentes: teste do corte escondido PULADO")
        return
    seg = 4
    for ext in (".mp4", ".mov"):
        for bf in (0, 2):
            rot = f"x {ext[1:].upper()} bf={bf}"
            tmp = T / "x" / f"gerado-bf{bf}{ext}"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            try:
                dados = video_com_fim_escondido(str(tmp), bf, seg)
            except Exception as e:
                checar(False, f"{rot}: gerar vídeo de teste", str(e))
                continue
            orig = gravar_original(T / "x" / f"cortado-bf{bf}{ext}", dados)
            exibe = duracoes_exibidas(orig)
            escondido = [t for t, dur in exibe.items() if dur <= seg / 2 + 0.2]
            if not checar(bool(escondido), f"{rot}: o original exibe só a primeira metade em pelo menos um fluxo", str(exibe)):
                continue
            saida = T / "x" / f"saida-bf{bf}-{ext[1:]}"
            rc, txt = rodar("limpar", orig, "--saida", saida)
            copia = saida / f"cortado-bf{bf}_limpo{ext}"
            if copia.is_file():
                # se o script entregou a cópia, ela não pode exibir mais do que o original exibia
                depois = duracoes_exibidas(copia)
                maior = {t: (exibe[t], depois.get(t)) for t in exibe if depois.get(t) is not None and depois[t] > exibe[t] + 0.15}
                checar(not maior, f"{rot}: a cópia entregue não exibe trecho que o original escondia", f"original e cópia: {maior}")
                checar(rc == 0 and "[ok] arquivo limpo" in txt, f"{rot}: cópia entregue vem com [ok]", txt[-300:])
            else:
                checar(rc != 0 and "[x]" in txt, f"{rot}: sem cópia, o script recusa com [x] e código diferente de 0", txt[-300:])
                checar("esconde" in txt, f"{rot}: a mensagem explica que o original esconde um trecho", txt[-300:])
                checar(ultima_linha_util(txt).startswith("não limpo:") and "Resumo: 0 de 1 arquivo(s) limpo(s)" in txt,
                       f"{rot}: resumo 0 de 1 com o arquivo listado como não limpo", txt[-300:])


def protegido(fn, *args):
    try:
        fn(*args)
    except Exception:
        checar(False, f"{fn.__name__}: erro inesperado no teste", traceback.format_exc().strip().splitlines()[-1])
        print(traceback.format_exc())


def main():
    if not SCRIPT.is_file():
        print(f"[FALHOU] não achei {SCRIPT}")
        return 1
    print(f"Script testado: {SCRIPT}")
    print(f"Python: {sys.version.split()[0]} | Pillow: {Image.__version__ if hasattr(Image, '__version__') else '?'} | ffmpeg: {FFMPEG or 'NÃO ENCONTRADO'}")
    T = Path(tempfile.mkdtemp(prefix="teste-limpar-"))
    try:
        for fn in (cenario_a, cenario_b, cenario_c, cenario_d, cenario_e, cenario_f, cenario_g, cenario_i_j_k,
                   cenario_l, cenario_m, cenario_n, cenario_o, cenario_p, cenario_q,
                   cenario_r, cenario_s, cenario_t, cenario_u, cenario_v, cenario_w, cenario_x):
            protegido(fn, T)
        protegido(cenario_h)
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print()
    if PULADOS:
        print(f"[AVISO] testes de vídeo pulados por falta do ffmpeg ({', '.join(PULADOS)}): vídeo NÃO foi verificado")
    if FALHAS:
        print(f"FALHARAM {len(FALHAS)} de {TOTAL} checagens:")
        for f in FALHAS:
            print(f"  - {f}")
        return 1
    print(f"TODOS OS TESTES PASSARAM ({TOTAL} checagens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
