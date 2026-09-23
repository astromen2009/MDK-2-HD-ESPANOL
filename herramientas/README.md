# Herramientas

Scripts usados para crear la traducción. **No son necesarios para jugar**: solo sirven si quieres modificar o mejorar la traducción.

Requieren **Python 3.8 o superior**. En Windows, usa `py` en lugar de `python`.

---

## mdk2_str_editor.py

Editor del archivo `mdk2.str`, que contiene todos los textos y subtítulos del juego.

Al abrirlo muestra una tabla con el ID de cada texto, el audio que lo acompaña, el texto de referencia (por ejemplo, el inglés) y el texto editable. Puedes buscar, filtrar y exportar o importar CSV para traducir en Excel. Al guardar recalcula todo el archivo, así que los textos pueden ser más largos o más cortos que el original.

No necesita instalar nada más.

**Interfaz gráfica:**

```
py mdk2_str_editor.py mdk2.str referencia.str
```

**Línea de comandos:**

```
py mdk2_str_editor.py info    mdk2.str
py mdk2_str_editor.py export  mdk2.str textos.csv  referencia.str
py mdk2_str_editor.py import  mdk2.str textos.csv  nuevo.str
py mdk2_str_editor.py verify  mdk2.str
```

La primera vez que sobrescribes un archivo crea una copia `.bak` automáticamente.

---

## mdk2_batch.py

Convierte las voces del MDK2 clásico al formato de audio de MDK2 HD. Acepta archivos sueltos o carpetas completas, y también archivos `.wav` normales.

**Necesita:**
- [ffmpeg](https://ffmpeg.org/) en el PATH.
- Para el modo `wma`: `xWMAEncode.exe`, incluido en el DirectX SDK (June 2010).

**Modo usado en esta traducción** (xWMA, el mismo formato y tamaño que los audios originales de la HD):

```
py mdk2_batch.py carpeta_clasico\sounds\ salida\ --modo wma --xwmaencode ruta\xWMAEncode.exe
```

**Modo sin pérdida** (PCM sin comprimir; solo necesita ffmpeg, pero los archivos pesan unas 7 veces más):

```
py mdk2_batch.py carpeta_clasico\sounds\ salida\
```

Por defecto la salida es de 22050 Hz mono, igual que las voces de la HD. Los archivos generados conservan el nombre original y se copian tal cual en la carpeta de sonidos del juego.
