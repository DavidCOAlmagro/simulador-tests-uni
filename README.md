# Test tipo autoescuela

Simulador de exámenes tipo test para estudiar las asignaturas de la carrera, al
estilo de los test de la autoescuela: modo práctica con explicaciones, modo
examen con tiempo y repaso de las preguntas falladas. Las preguntas se escriben
en ficheros .txt muy sencillos, así que cualquiera puede añadir las suyas.

Trae bancos de preguntas de Sistemas Distribuidos y de Arquitectura de
Computadores (laboratorio de OpenMP, en inglés).

En Windows basta con tener Python 3.10 o superior de python.org y hacer doble
clic en `lanzar.bat`. En Linux y Mac:

```sh
./lanzar.sh                    # abre la app con la última asignatura usada
./lanzar.sh otro_banco.txt     # además carga ese fichero
./lanzar.sh --zoom 1.3         # letra más grande
./lanzar.sh --check            # valida los .txt de todas las asignaturas sin abrir ventana
```

Hace falta Python con tkinter. `lanzar.sh` usa el venv de la carpeta (`.venv`) si
existe; si no, el entorno `ice` de micromamba o el `python3` del sistema. Para que las fuentes se vean suavizadas, el Tk del
entorno debe ser la compilación con Xft:

```sh
micromamba install -n ice -c conda-forge 'tk=8.6.13=xft*'
```

Hay tema claro y oscuro (botón arriba a la derecha en la pantalla de inicio);
la preferencia se guarda en `ajustes.json`.

## Asignaturas

Cada asignatura es una carpeta dentro de `preguntas/` (por ejemplo
`preguntas/Sistemas Distribuidos/` y `preguntas/ArCO/`), con sus .txt. Arriba en la
pantalla de inicio se elige la asignatura, y «Nueva asignatura…» crea otra.
«Añadir fichero…» copia el .txt a la carpeta de la asignatura actual, para que se
cargue siempre con ella.

Los fallos se guardan por asignatura en `fallos/<asignatura>/`:

- `fallos.json`: las preguntas pendientes de repasar (salen al acertarlas).
- `historial_fallos.json`: todo lo que has fallado alguna vez; no se borra.

## Modos

- **Práctica**: corriges cada pregunta al momento y ves la explicación. Con
  «← Anterior», las flechas o la cuadrícula puedes volver a las ya corregidas.
- **Examen**: tiempo límite y cuadrícula de preguntas para saltar entre ellas.
  Se corrige todo al entregar (o al acabarse el tiempo).
- **Repaso de fallos**: como práctica, pero solo con las preguntas que has
  fallado o dejado en blanco en la asignatura elegida. Cada pregunta sale de
  la lista cuando la aciertas.

Nota = (aciertos − fallos / N) / preguntas × 10, donde N es la penalización
(cada N fallos restan un acierto). Las preguntas en blanco no restan. Una
pregunta de respuesta múltiple solo cuenta como acierto si marcas exactamente
las opciones correctas.

Teclas: `a`–`h` o `1`–`9` eligen opción, `Enter` corrige o avanza, `←`/`→`
navegan entre preguntas (también en práctica, para repasar las ya corregidas), `Supr` borra la respuesta y `Ctrl+C` (o el botón «Copiar
pregunta») copia la pregunta con sus opciones, y también la solución y la
explicación si ya está corregida.

## Formato de los .txt

```
% comentario (líneas que empiezan por %)
@titulo: Teoría SSDD - Prueba 1      ← opcionales, al principio del fichero
@tiempo: 30                          ← minutos que se proponen para el examen
@penalizacion: 3                     ← cada 3 fallos restan 1
@preguntas: 15                       ← número de preguntas que se propone
@reparto: Tema 1=4; Tema 2=4; Tema 3=5   ← opcional: cuántas preguntas de cada tema
                                        (por el inicio del nombre del tema)

T: Tema 3 - Invocación remota         ← tema (opcional). Vale para esta pregunta
                                        y las siguientes, hasta el próximo T:
P: Texto de la pregunta.
Puede ocupar varias líneas.
a) Primera opción
b) Segunda opción
   (una opción también puede ocupar varias líneas)
c) Tercera opción
R: b                                  ← en respuesta múltiple: R: a, c
E: Explicación. Puede ocupar
varias líneas, hasta la siguiente P: o ---
---
P: Siguiente pregunta...
```

- `---` separa preguntas. Es opcional, porque una nueva `P:` ya empieza otra
  pregunta, pero ayuda a leer el fichero.
- Para código, ponlo entre líneas con ```` ``` ````: se verá en letra
  monoespaciada y dentro no se interpreta nada (ni `%`, ni `a)`, ni `---`).
  `` `así` `` marca código dentro de una línea.
- Las letras pueden ir en mayúscula o minúscula: `A)` o `a)`.
- Si alguna opción hace referencia a otras («todas las anteriores», «a y b»…),
  las opciones de esa pregunta no se barajan.
- Las preguntas mal escritas se saltan y aparecen como avisos, con el fichero
  y la línea, en el botón «Ver avisos» o con `--check`.

## Créditos

Proyecto de David Calzado Olmo, hecho con [Claude](https://claude.ai) (Anthropic)
y con la colaboración de [Rodrigo](https://github.com/RodrigoMunozMartin).

