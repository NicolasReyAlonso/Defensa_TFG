# Puntero: mando de Wii para presentaciones

Un único script, `presenter.py`, que mientras está en marcha mueve el cursor
apuntando con el mando y convierte sus botones en teclas. Se lanza, funciona y
se cierra: no instala servicios ni drivers ni escribe fuera de esta carpeta.

> **Estado de las pruebas.** Sin mando físico delante solo se ha podido probar
> la lógica (decodificación de informes, seguimiento, homografía, suavizado, UDP)
> y los mensajes de error. **No se ha probado todavía** la conexión Bluetooth
> real, la cámara IR ni la inyección por `/dev/uinput`. En macOS, lo probado y
> lo pendiente está en [su sección](#macos).

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

En Linux no hace falta `.venv`; solo serviría para el backend `pynput`
(`.venv/bin/pip install pynput`, requiere `sudo apt install python3-dev`).

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
| B (mantener) | Puntero láser: mantiene pulsada la tecla L, y la presentación (`../Presentation`) cambia el cursor por un punto rojo que deja estela al moverse |
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

El mismo script funciona en Mac de forma nativa. Lo que cambia es cómo se
habla con el mando y cómo se mueve el ratón:

- **Leer el mando.** Desde macOS 12 ya no se pueden abrir los canales L2CAP
  del mando como hace Linux (Dolphin quitó ese código por eso). macOS gestiona
  el Bluetooth y expone el mando como dispositivo HID, y el script lo lee con
  `hidapi`, igual que Dolphin.
- **Emparejar.** Desde Ajustes > Bluetooth no se puede: macOS pide un PIN que
  no se puede teclear, porque es una dirección Bluetooth en binario.
  `--pair` hace lo mismo que [WiimotePair](https://github.com/dolphin-emu/WiimotePair),
  de Dolphin: con el botón **SYNC** el PIN es la dirección del Mac al revés, y
  se entrega con dos llamadas privadas de IOBluetooth.
- **Ratón y teclado.** Eventos `CGEvent` de Quartz (pyobjc), sin `pynput`.
  Con un botón pulsado genera eventos de arrastre, que es lo que espera macOS.

### Instalación (una vez)

Python de Homebrew no deja instalar con `pip` fuera de un entorno virtual:

```bash
cd puntero
python3 -m venv .venv
source .venv/bin/activate        # en cada terminal nuevo
pip install -r requirements.txt  # hidapi y pyobjc (IOBluetooth, CoreBluetooth, Quartz)
```

### Permisos

macOS los concede a la app desde la que lanzas el script (Terminal, iTerm,
VS Code), no a Python:

- **Bluetooth**: solo para `--pair`. La primera vez sale el aviso.
- **Accesibilidad**: para mover el cursor y pulsar teclas. Sin ella macOS
  descarta los eventos en silencio, así que el script lo comprueba al arrancar.
  Si falta, abre el aviso y sale. Actívala en Ajustes > Privacidad y seguridad >
  Accesibilidad y vuelve a lanzarlo.

### Uso

```bash
python3 presenter.py --pair        # una vez por mando
python3 presenter.py --calibrate   # una vez por sala y posición
python3 presenter.py               # presentar
```

**Emparejar:** lanza `--pair`, abre la tapa de las pilas y pulsa una vez el
botón rojo **SYNC** (no 1+2). Al terminar, pulsa cualquier botón del mando y se
conecta solo, o usa «Conectar» en Ajustes > Bluetooth. A partir de ahí no hace
falta repetirlo: antes de calibrar o presentar basta con pulsar un botón del
mando. El script espera hasta 20 s a que aparezca (`--scan-time`). `--mac` no
se usa en macOS.

**Proyector:** si la pantalla está extendida (no en espejo), el script lista
las pantallas al arrancar y usa la principal. Elige la del proyector con
`--display 2`. En espejo no hace falta.

**Si el emparejamiento falla** (avisos del sistema a medias, errores de
IOBluetooth): `sudo pkill bluetoothd` o reiniciar el Mac suele desbloquearlo.
Si al reconectar macOS pide un PIN, olvida el mando en Ajustes > Bluetooth y
repite `--pair`. Son los remedios que se citan en el
[issue #8 de WiimotePair](https://github.com/dolphin-emu/WiimotePair/issues/8)
para macOS 15.

### Estado de las pruebas en macOS

Probado en macOS 27.0.1 (Apple Silicon, Python 3.14), **sin mando**:

- Las dependencias se instalan, y las APIs privadas que usa `--pair` existen en
  esta versión con las firmas esperadas.
- El formato de los informes por HID (y por L2CAP en Linux) es correcto con
  dispositivos simulados: inicialización de la cámara IR, LEDs, vibración,
  botones, puntos IR y batería.
- Los eventos de ratón y teclado se construyen bien (tipo, posición, tecla,
  arrastre), pero no se han llegado a enviar porque el entorno de prueba no
  tenía Accesibilidad.
- Los mensajes cuando no hay mando, calibración o dependencias.

**Sin probar:** el emparejamiento real y que macOS haga llegar al mando las
órdenes de salida (LEDs, cámara IR), sobre todo en un RVL-CNT-01-TR. Si el
mando conecta pero el script se queda sin puntos IR, ese es el sospechoso.

### Alternativa: el mando en un Linux y el Mac por red

Si no consigues conectar el mando al Mac, un equipo Linux puede leerlo y
mandar posición, teclas y clics por UDP (puerto 9999):

```bash
# En el Mac (con el .venv activado y Accesibilidad concedida)
python3 presenter.py --listen

# En el Linux con el mando
python3 presenter.py --send IP_DEL_MAC
```

El envío y la recepción están probados en local. El receptor acepta paquetes
de cualquier equipo de la red, así que úsalo solo en redes de confianza.
