# Puntero: mando de Wii para presentaciones

Un único script, `presenter.py`, que mientras está en marcha mueve el cursor
apuntando con el mando y convierte sus botones en teclas. Se lanza, funciona y
se cierra: no instala servicios ni drivers ni escribe fuera de esta carpeta.

> **Estado de las pruebas.** Sin mando físico delante solo se ha podido probar
> la lógica (decodificación de informes, seguimiento, homografía, suavizado, UDP)
> y los mensajes de error. **No se ha probado todavía** la conexión Bluetooth
> real, la cámara IR ni la inyección por `/dev/uinput`.

## Dependencias (Ubuntu)

- Python 3 y BlueZ (`bluetoothctl`), ambos ya instalados en Ubuntu de serie.
- Ningún paquete apt ni pip adicional.

`libcwiid` no se usa: el script abre directamente los dos canales L2CAP del
mando con el módulo `socket` de Python, que es lo mismo que hace `libcwiid` por
dentro. Tampoco se usa `pynput` en Linux: esta máquina (Ubuntu 26.04) solo tiene
sesión Wayland, donde `pynput` no inyecta eventos, y además no llega a
instalarse (su dependencia `evdev` no compila sin `python3-dev`). En su lugar se
crean un teclado y un puntero virtuales con `/dev/uinput`, que funciona igual en
Wayland y en X11.

El único paso con privilegios es dar acceso a `/dev/uinput`, y dura solo hasta
el siguiente reinicio:

```bash
sudo setfacl -m u:$USER:rw /dev/uinput
```

El `.venv` está creado pero vacío; solo sirve si algún día se quiere el backend
`pynput` (`.venv/bin/pip install pynput`, requiere `sudo apt install python3-dev`).

## Uso

```bash
python3 presenter.py --calibrate   # una vez por sala y posición
python3 presenter.py               # presentar
```

**Emparejar:** no hay emparejamiento persistente. Al lanzar el script, pulsa
**1+2** a la vez en el mando (los cuatro LEDs parpadean); el script lo busca
durante 6 s y se conecta. Si ya conoces la dirección, `--mac XX:XX:XX:XX:XX:XX`
se salta la búsqueda. No lo emparejes desde los ajustes de Bluetooth de GNOME.

**Calibrar:** colócate donde vayas a presentar, apunta a cada esquina de la
pantalla en el orden que pide el terminal (superior izquierda, superior
derecha, inferior derecha, inferior izquierda) y pulsa **A** en cada una. Los
LEDs indican el paso y el mando vibra al registrar la esquina. El resultado se
guarda en `calibration.json`; repetir `--calibrate` lo sobrescribe. Home cancela.

## Botones

| Botón | Acción |
|---|---|
| Cruceta derecha | Flecha derecha (siguiente) |
| Cruceta izquierda | Flecha izquierda (anterior) |
| A | Clic izquierdo (se mantiene mientras se pulsa) |
| Home | Salir: suelta teclas y botones, apaga IR y LEDs, cierra Bluetooth |
| B (mantener) | Puntero láser: mantiene pulsada la tecla L, y la presentación (`../Presentation`) cambia el cursor por un punto rojo |
| Arriba, Abajo, +, −, 1, 2 | Libres |

El mapeo es la tabla `BUTTON_MAP` al principio de `presenter.py`.

## Ajustes

En `CONFIG`, al principio del fichero, o por línea de comandos:

| Opción | Por defecto | Efecto |
|---|---|---|
| `--smoothing ema\|mean` | `ema` | Filtro exponencial o media móvil |
| `--alpha` | 0.25 | EMA: más bajo, más suave y con más retardo |
| `--window` | 8 | Media móvil: número de muestras |
| `--lost-frames` | 30 | Frames (~100 Hz) sin ver la barra antes de exigir de nuevo los dos puntos |
| `--backend dry` | | No toca el sistema, imprime lo que haría (para probar el mando) |

Si se pierde la barra, el cursor se queda quieto; al recuperarla se desliza
hasta la nueva posición en vez de saltar. Con un solo LED visible se sigue
moviendo usando la última geometría conocida.

## Cómo funciona el apuntado

El punto de apuntado es el punto medio de los dos focos de la barra, con el eje
X invertido (la cámara ve en espejo) y corregido por el giro de muñeca. Las
cuatro esquinas calibradas definen una homografía que lleva ese punto a la
pantalla; se resuelve en Python puro (sistema 8×8), sin OpenCV ni numpy.

## Limitaciones conocidas

- La calibración vale para la posición desde la que se hizo. Si te mueves
  mucho por la sala, el cursor se desvía.
- La cámara tiene unos 40° de campo: muy cerca de la pantalla, las esquinas
  pueden quedar fuera de su vista.
- Con varios monitores, el puntero absoluto de `uinput` cubre lo que el
  compositor decida (normalmente todo el escritorio). Sin probar.
- Luz solar, velas o halógenos se ven como puntos IR falsos.
- Los mandos nuevos (RVL-CNT-01-TR) pueden apagarse si rechazan la conexión
  sin autenticar. Sin probar.
- La barra de sensores se supone que solo recibe corriente por USB. Para
  comprobarlo, ejecuta `lsusb` antes y después de enchufarla: si aparece un
  dispositivo nuevo, no es una barra pasiva (por ejemplo, una Mayflash
  DolphinBar lleva su propio Bluetooth) y habría que replantear la conexión.

## macOS

**Lectura directa del mando en Mac: no intentada, no hay Mac en este entorno.**
No se ha escrito código de IOBluetooth/pyobjc porque no se podría verificar.
Como referencia, sin comprobar aquí: macOS no ofrece en su interfaz la forma de
emparejar el mando y gestiona él mismo los canales HID, por lo que el acceso
por L2CAP desde Python es dudoso en versiones recientes.

Lo que sí está hecho es la alternativa cliente-servidor, en el mismo script:

```bash
# En el Mac (pip install pynput; conceder Accesibilidad e Input Monitoring al terminal)
python3 presenter.py --listen

# En el Linux con el mando
python3 presenter.py --send IP_DEL_MAC
```

El Linux lee el mando y envía posición, teclas y clics por UDP (puerto 9999);
el Mac los aplica con `pynput`. El envío y la recepción están probados en
local; el lado `pynput` en un Mac real, no. El receptor acepta paquetes de
cualquier equipo de la red, así que úsalo solo en redes de confianza.
