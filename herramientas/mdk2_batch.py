#!/usr/bin/env python3
"""
mdk2_batch.py - convierte archivos del MDK2 clasico al formato de MDK2 HD.

Acepta como entrada:
  - archivos del MDK2 clasico (WAVC V1.0 con Interplay ACM dentro)
  - archivos WAV normales (cualquier formato que lea ffmpeg)
  - carpetas enteras con cualquiera de los dos

Produce archivos listos para copiar en la carpeta del juego HD.

Dos modos de salida:

  --modo pcm   (por defecto)  Codec 0: 8 bytes de cabecera + PCM 16 bits crudo.
                              Sin perdida. Solo necesita ffmpeg.
                              Los archivos pesan unas 7 veces mas.

  --modo wma                  Codec 3: xWMA, igual que los originales del juego.
                              Necesita ffmpeg Y xWMAEncode.exe del DirectX SDK
                              (June 2010).

Ejemplos (Windows, usa "py" en vez de "python3"):

    py mdk2_batch.py doblaje.wav salida\\
    py mdk2_batch.py C:\\clasico\\sounds\\ salida\\
    py mdk2_batch.py C:\\clasico\\sounds\\ salida\\ --modo wma --xwmaencode C:\\tools\\xWMAEncode.exe

Por defecto todo sale a 22050 Hz mono, que es lo que usan las voces del HD.
"""
import argparse, glob, os, struct, subprocess, sys, tempfile, shutil

WAVC = b"WAVC"


# ---------------------------------------------------------------- utilidades

def find_tool(name, override=None):
    if override:
        if not os.path.isfile(override):
            sys.exit("No existe: %s" % override)
        return override
    p = shutil.which(name) or shutil.which(name + ".exe")
    if not p:
        sys.exit("No encuentro '%s' en el PATH. Instalalo o pasa la ruta con "
                 "--ffmpeg / --xwmaencode." % name)
    return p


def run(args):
    r = subprocess.run(args, capture_output=True)
    if r.returncode:
        raise RuntimeError(" ".join(str(a) for a in args) + "\n" +
                           r.stderr.decode(errors="replace").strip())
    return r


def read_riff_chunks(path):
    d = open(path, "rb").read()
    out, i = {}, 12
    while i < len(d) - 8:
        cid = d[i:i + 4]
        sz = struct.unpack_from("<I", d, i + 4)[0]
        out[cid] = d[i + 8:i + 8 + sz]
        i += 8 + sz + (sz & 1)
    return out


# ---------------------------------------------------------------- entrada

def to_pcm(src, dst, ffmpeg, rate, ch):
    """Deja el origen como WAV PCM 16 bits en dst. Maneja WAVC/ACM y WAV."""
    d = open(src, "rb").read()
    tmp_acm = None
    try:
        if d[:4] == WAVC:
            hs = struct.unpack_from("<I", d, 16)[0]
            tmp_acm = tempfile.mktemp(suffix=".acm")
            open(tmp_acm, "wb").write(d[hs:])
            inp = tmp_acm
        elif d[:4] == b"RIFF":
            inp = src
        elif d[0] in (0, 1, 2, 3):
            raise RuntimeError("parece un archivo de MDK2 HD, no del clasico. "
                               "Este script solo convierte audio clasico o WAV.")
        else:
            inp = src
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", inp,
             "-c:a", "pcm_s16le", "-ar", str(rate), "-ac", str(ch), dst])
    finally:
        if tmp_acm and os.path.exists(tmp_acm):
            os.remove(tmp_acm)


# ---------------------------------------------------------------- salida

def write_codec0(pcm_wav, dst):
    c = read_riff_chunks(pcm_wav)
    fmt, data = c[b"fmt "], c[b"data"]
    _, ch, rate = struct.unpack_from("<HHI", fmt, 0)
    h = bytearray(8)
    h[0] = 0
    h[1] = ch
    struct.pack_into("<H", h, 2, rate)
    struct.pack_into("<I", h, 4, len(data) // (2 * ch))
    open(dst, "wb").write(bytes(h) + data)
    return len(data) // (2 * ch)


def write_codec3(pcm_wav, dst, xwmaencode, bitrate):
    c = read_riff_chunks(pcm_wav)
    _, ch, _ = struct.unpack_from("<HHI", c[b"fmt "], 0)
    nsamples = len(c[b"data"]) // (2 * ch)
    tmp = tempfile.mktemp(suffix=".xwma")
    try:
        run([xwmaencode, "-b", str(bitrate), pcm_wav, tmp])
        x = read_riff_chunks(tmp)
        fmt, dpds, data = x[b"fmt "], x.get(b"dpds"), x[b"data"]
        if dpds is None:
            raise RuntimeError("xWMAEncode no genero tabla dpds")
        tag, ch2, rate, avg, ba, bits = struct.unpack_from("<HHIIHH", fmt, 0)
        if tag != 0x0161:
            raise RuntimeError("wFormatTag 0x%04x, el juego espera 0x0161" % tag)
        n = len(dpds) // 4
        if len(data) < n * ba:
            data += b"\0" * (n * ba - len(data))
        data = data[:n * ba]
        h = bytearray(0x2C)
        h[0] = 3
        h[1] = ch2
        struct.pack_into("<H", h, 2, rate)
        struct.pack_into("<I", h, 4, nsamples)
        struct.pack_into("<H", h, 8, 0x0161)
        struct.pack_into("<I", h, 10, avg)
        struct.pack_into("<H", h, 14, ba)
        struct.pack_into("<H", h, 16, 16)
        struct.pack_into("<I", h, 0x28, n)
        open(dst, "wb").write(bytes(h) + dpds + data)
        return nsamples, n, ba
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------- principal

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entrada", help="archivo o carpeta de origen")
    ap.add_argument("salida", help="carpeta de destino")
    ap.add_argument("--modo", choices=["pcm", "wma"], default="pcm")
    ap.add_argument("--rate", type=int, default=22050)
    ap.add_argument("--canales", type=int, default=1)
    ap.add_argument("--bitrate", type=int, default=20000,
                    help="bitrate WMA en bps (solo --modo wma)")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--xwmaencode")
    a = ap.parse_args()

    ffmpeg = find_tool("ffmpeg", a.ffmpeg)
    xwma = find_tool("xWMAEncode", a.xwmaencode) if a.modo == "wma" else None

    if os.path.isdir(a.entrada):
        files = sorted(f for f in glob.glob(os.path.join(a.entrada, "*"))
                       if os.path.isfile(f))
    else:
        files = [a.entrada]
    if not files:
        sys.exit("No hay archivos en %s" % a.entrada)
    os.makedirs(a.salida, exist_ok=True)

    ok = fail = 0
    for src in files:
        name = os.path.basename(src)
        base = os.path.splitext(name)[0]
        dst = os.path.join(a.salida, base + ".wav")
        tmp = tempfile.mktemp(suffix=".wav")
        try:
            to_pcm(src, tmp, ffmpeg, a.rate, a.canales)
            if a.modo == "pcm":
                ns = write_codec0(tmp, dst)
                print("  OK  %-28s codec 0, %d muestras, %d bytes"
                      % (name, ns, os.path.getsize(dst)))
            else:
                ns, n, ba = write_codec3(tmp, dst, xwma, a.bitrate)
                print("  OK  %-28s codec 3, %d muestras, %d paquetes de %d, %d bytes"
                      % (name, ns, n, ba, os.path.getsize(dst)))
            ok += 1
        except Exception as e:
            print("  ERR %-28s %s" % (name, str(e).splitlines()[0]))
            fail += 1
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    print("\n%d convertidos, %d con error -> %s" % (ok, fail, a.salida))
    if a.modo == "pcm":
        print("Modo PCM: sin perdida, pero los archivos pesan mas que los originales.")


if __name__ == "__main__":
    main()
