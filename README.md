# MAGIC ZIP 2.0 — powered by FiscalOps · by LAVA

Aplicación local para Windows. Prepara expedientes ZIP con PDF, Excel, PowerPoint y otros formatos. La ruta recomendada se inicia con `INICIAR_MAGICZIP.bat`, Python y el navegador local; no requiere ejecutable compilado.

## Uso

1. Ejecuta **INICIAR_MAGICZIP.bat** y elige la carpeta matriz en el navegador local.
2. La siguiente pantalla muestra sus carpetas de primer nivel. Marca las que quieras procesar, configura el máximo por ZIP (4 MB por defecto) y el formato de sus nombres.
3. Pulsa **Crear ZIPs finales**. Cada carpeta se analiza, prepara, comprime y verifica antes de pasar a la siguiente. Su animación muestra la etapa real y, al terminar, los nombres definitivos de sus ZIP.

Si solo se genera un ZIP, su nombre es el de la carpeta (por ejemplo, `ANEXO 1.zip`). Para varias partes, los formatos disponibles son `ANEXO 1_1-3.zip`, `ANEXO 1_PARTE 1-3.zip` y `ANEXO 1_PARTE 1 DE 3.zip`. El total corresponde a las partes realmente generadas de esa carpeta, también si hay pendientes. Las particiones PDF usan siempre `Documento_1-X.pdf`, `Documento_2-X.pdf`, etc., tanto en el flujo ZIP como en Magic PDF. El informe conserva los rangos de páginas. Se conservan las subcarpetas dentro de los ZIP. Los archivos que estén directamente en la matriz se agrupan con el nombre de esa matriz.

La salida es `<destino elegido>/ZIPS FINALES/Envio_fecha_hora/`. **Esa carpeta contiene únicamente los ZIP finales.** Cada ejecución es independiente. Si eliges directamente una carpeta llamada ZIPS FINALES como destino, no se duplica el nombre. Estas carpetas se excluyen del análisis. La distribución de archivos usa best-fit decreasing por carpeta: intenta llenar cada ZIP lo más cerca posible del límite, sin mezclar carpetas ni superar los 4,000,000 bytes; el tamaño real del ZIP siempre manda sobre la estimación.

La pantalla **Resultados** despliega todos los ZIP sin listas plegables. Cada archivo muestra su icono y peso en MB. Cada carpeta muestra el peso de origen, el peso final agregado de sus ZIP y un desplegable solo si hay archivos pendientes o problemas que requieren una decisión. Cuando todo está incluido no se muestran avisos por optimizar, dividir, convertir o aplicar blanco y negro; esos cambios se consultan en Detalle. Los ZIP y sus pesos permanecen siempre desplegados. **Carpeta fuente** abre el origen de esa carpeta en el Explorador de Windows y **Ver ubicación** abre el directorio del archivo pendiente. Estos accesos conservan la ubicación correspondiente a cada ejecución, aunque después cambies de selección. Al agregar archivos desde el navegador no se recibe su ruta original; usa **Examinar en Windows** para habilitar estos accesos. **Detalle** permite revisar cambios, conversiones y verificaciones de la sesión. **Nuevo ZIP** limpia selección, resultados y copias temporales para comenzar de nuevo; conserva los originales y los ZIP entregados. Los ajustes de tamaño y nombres se conservan como preferencias.

Al cerrar la última ventana, el motor espera unos segundos para permitir recargas, cancela cualquier trabajo pendiente de forma segura y limpia los temporales. Una ventana de terminal que siga abierta mantiene la sesión; `Ctrl+C` apaga el motor para liberar memoria.

También puedes agregar archivos sueltos o arrastrarlos. Si no has elegido destino, la aplicación lo pedirá antes de procesar. El tamaño se interpreta en MB decimales: **4 MB = 4,000,000 bytes**. Cada ZIP debe medir estrictamente menos que el máximo. Son ZIP independientes, no volúmenes `.z01`.

## Qué hace automáticamente

- **PDF:** intenta optimización sin pérdida y, si todavía no cabe, crea bloques de páginas completos. Prefiere separaciones de marcadores cuando aprovechan el espacio. Reserva margen para cabeceras del ZIP y verifica el peso real.
- **Excel:** intenta una copia más ligera sin modificar datos ni fórmulas. Primero recomprime el paquete conservando cada archivo interno byte a byte. En XLSX compatibles puede probar una copia XLSB mediante Excel nativo. Si un XLSX supera 4 MB y está activa la opción correspondiente, crea una copia por pestaña, convierte fórmulas a sus valores actuales y usa la regla `ARCHIVO_1-X.xlsx`; el original y sus fórmulas permanecen intactos.
- **PowerPoint:** exporta una copia estática a PDF con PowerPoint nativo, incluye diapositivas ocultas y comprueba su número de páginas. Después aplica el mismo flujo PDF de optimización y división.
- **Word:** exporta DOC/DOCX y formatos compatibles a PDF con una instancia aislada de Word; después aplica el mismo flujo PDF de optimización y división.
- **Otros formatos:** intenta compresión ZIP real. Si no caben, aparecen como pendientes con recomendaciones según el formato.
- **Integridad:** vuelve a leer los ZIP y compara SHA-256 de sus entradas con los originales intactos o las copias preparadas verificadas. El informe mantiene la relación con cada fuente original.

No se borran ni sobrescriben originales. No se eliminan filas, hojas, imágenes o fórmulas para reducir tamaño. Las copias de trabajo permanecen en la sesión temporal, separadas de la entrega, y se eliminan al limpiar o cerrar.

## Excel: condiciones de conservación

La recompresión sin pérdida verifica todos los elementos internos, incluidos VBA y objetos, sin ejecutarlos. Los libros con firmas detectadas no se recodifican.

La conversión XLSB automática se limita a **XLSX que puedan verificarse**. Se utiliza una instancia aislada de Excel sobre una copia, con macros y actualizaciones de vínculos desactivadas, sin recalcular antes de guardar. Se contrastan valores y fórmulas por posición, orden/nombres/visibilidad de hojas, rangos usados y nombres definidos. La copia XLSB se reabre y se vuelve a comprobar; una exportación de retorno a XLSX también se compara con el contenido, estilos y estructura de origen.

Solo se utiliza XLSB si todas las comprobaciones pasan **y es más pequeño** que la mejor alternativa. Si hay discrepancias, se descarta XLSB y se usa la copia sin pérdida o el original. No se promete una conversión universal: libros con gráficos, tablas, conexiones, pivotes, macros, texto enriquecido u otros elementos complejos conservan su formato. El límite de verificación binaria es 128 MiB descomprimidos y 500,000 celdas de rango usado por hoja. Los XLS antiguos no se convierten automáticamente; los XLSM/XLSB pueden beneficiarse de la recompresión exacta de sus elementos.

## PowerPoint, Word y PDF

Se necesita PowerPoint instalado. Para proteger el trabajo abierto, si PowerPoint ya se está usando, la conversión queda pendiente con instrucciones; no se cierra una presentación personal. Los vínculos externos a contenido que pudiera faltar requieren revisión previa.

El PDF conserva diapositivas estáticas. Notas del presentador, animaciones, audio y contenido interactivo siguen en el PPT original. Un PDF puede ser más grande que el PPT; el objetivo del flujo posterior es crear adjuntos bajo el límite, no garantizar ahorro total en todos los formatos.

Los PDF con firmas detectadas o cifrados se empaquetan intactos, sin editar. La detección de firma no valida su vigencia criptográfica. Si una sola página no cabe, queda identificada como pendiente; no se corta una página por la mitad.

La reducción adicional de imágenes sigue siendo opcional. Cuando se habilita, el optimizador clasifica cada página como digital, escaneada o mixta, y elige el objetivo de resolución por página: mantiene más margen en escaneos porque el texto está formado por píxeles, y permite objetivos menores en páginas digitales porque texto y vectores no se rasterizan. Nunca reduce una imagen que ya está cerca del objetivo efectivo de la página. Las etapas se prueban en orden: estructuras y streams sin pérdida, imágenes sin pérdida, recomprensión selectiva, resolución adaptativa, escala de grises para imágenes sin color, blanco y negro adaptativo para escaneos de texto y una última reducción opcional.

Cada candidato se conserva solo si reduce realmente el tamaño y pasa la validación: apertura, número de páginas, texto, geometría, rotación, anotaciones, formularios y comparación visual renderizada. El PDF no se rasteriza completo. La detección de color evita convertir sellos, logotipos y gráficos relevantes a gris; la binarización solo se intenta en imágenes que parecen escaneos de texto. La resolución se calcula con el tamaño de la imagen y el área de la página, no con los metadatos DPI del archivo, que pueden ser engañosos. Revisar siempre folios, sellos, códigos QR y letra pequeña antes de enviar.

GemBox puede ser útil como motor .NET alternativo (Spreadsheet para Excel/XLSB/PDF y Document para Word/PDF), pero no se incluye como dependencia obligatoria ni se asume una licencia. La ruta integrada usa Office local para conversiones que requieren fidelidad y conserva el fallback seguro si Office no está disponible.

## Decisiones PDF adaptativas

Los libros extensos que exceden el límite usan una ruta de división directa antes de intentar recomprimir el documento completo. Se copian páginas y recursos sin recodificar texto, fuentes, imágenes ni vectores; se miden las partes reales y se verifican sus objetos, geometría y apertura/renderizado. Los enlaces del índice se conservan y los destinos entre partes se convierten en enlaces relativos: extrae todas las partes en una misma carpeta para utilizarlos. Los marcadores globales y las etiquetas de accesibilidad del libro completo permanecen en el original. Solo una página que no cabe por sí sola pasa al optimizador intensivo. Esta ruta también se usa en Dividir y en Comprimir cuando se permite dividir automáticamente.

La interfaz muestra acciones en español y progreso por páginas. Se evita comprimir un ZIP de prueba para cada candidato con margen suficiente; el ZIP definitivo siempre se mide y verifica. El autodiagnóstico y benchmark del libro de referencia se encuentran en `work/book-pdf-audit`. La entrega que incorpora esta mejora está en `dist/MagicZip_2.0_BookFast_Win64`.

Se analizan todos los PDF: bytes reales, páginas, texto seleccionable, vectores, imágenes, dimensiones y resolución estimada para una imagen que ocupara toda la página. La clasificación digital/escaneado/mixto orienta el diagnóstico; no es una certificación OCR.

Un PDF válido de menos del límite configurado se acepta con su nombre y bytes originales, sin optimización ni avisos por tamaño. No se utiliza la reserva estimada del ZIP para forzar su edición. **4 MB = 4,000,000 bytes**; 3,945 KiB de Windows son 4,039,680 bytes. PDF y ZIP se verifican por separado: ambos deben medir estrictamente menos que el máximo. Un ZIP pequeño no permite entregar un PDF que exceda su propio límite.

Para PDF que exceden el límite: compresión de recursos sin pérdida, recompresión exacta de imágenes, optimización de imágenes, resolución adaptativa, escala de grises selectiva, binarización adaptativa y, solo con el ajuste correspondiente, reducción adicional. Se detiene al cumplir el objetivo. Cada candidato debe reducir realmente el tamaño y conservar texto, geometría, rotación, anotaciones y valores de formularios; también se renderizan todas sus páginas para comparar detalle y color. No se rasterizan las páginas digitales. Se omiten imágenes con máscaras, espacios de color especiales o más de 40 megapíxeles; las imágenes con color detectado conservan su color. La comparación automática no garantiza lectura humana ni decodificación de QR: revisa las copias con cambios de imagen.

Se conservan firmas digitales, cifrado y formularios sin transformarlos. Si exceden el límite, se informa la restricción y el original permanece intacto. Si es necesario dividir, se miden PDF válidos con páginas completas y se intenta optimizar individualmente una página que no cabe. Los pendientes incluyen bytes originales/procesados, página, límite, intentos y alternativas. No se activa segmentación visual sin autorización explícita. El límite de edición sigue siendo 512 MiB.

Las decisiones y etapas están en **Detalle**, dentro de desplegables, y en el informe JSON/CSV. Un solo ZIP conserva el nombre de la carpeta (`ANEXO 1.zip`); varias partes usan los formatos numerados existentes. El contenido comienza directamente en la raíz del ZIP y conserva sus subcarpetas, incluidas las vacías.

## Detalle de la sesión

El informe HTML, la trazabilidad JSON, los registros CSV internos y las copias preparadas se guardan en `work/sessions/<sesión>/`, fuera de la carpeta de entrega. No aparecen como archivos finales. El informe HTML se abre desde **Detalle**. Estos datos dejan de estar disponibles al limpiar o cerrar la sesión; los ZIP finales se conservan.

Errores, trabajo parcial, cancelaciones y archivos que no caben se reflejan en las alertas. La validación técnica no acredita aceptación del contenido por el SAT: confirma los formatos admitidos por tu trámite.

## Interfaz y efectos

Inicio Matrix/cyberpunk con lluvia de números y luces de neón. Puedes entrar con Enter o Escape y repetirlo desde **Matrix**. Las animaciones se activan automáticamente, sin interruptor de efectos. Se respeta el movimiento reducido del sistema y se detiene el canvas cuando está oculto. El logo animado combina carpeta y rayo en SVG, con iconos distintos para carpetas, ZIP, PDF, Excel, PowerPoint, imágenes y otros archivos. La interfaz usa nombres y cifras principales de al menos 13 px en PC, texto secundario de 11–12 px y botones con foco visible. Se ajusta a portátiles y monitores de PC, incluidas ventanas con menos altura y escalas ampliadas. Los encabezados de carpeta conservan el peso final visible durante el desplazamiento. El detalle de archivos se construye al abrir su pestaña.

El procesamiento ocupa toda la ventana del navegador. Conserva a la vista el límite y formato de nombres elegidos, muestra la etapa real y el tiempo, y anima cada carpeta con sus salidas verificadas y pesos finales en MB. El movimiento indica actividad, no un porcentaje global simulado. El porcentaje visible corresponde únicamente a la etapa actual. La selección de carpeta tiene una escena de conexión propia, seguida por la entrada escalonada de sus carpetas. La intro abre y cierra con transiciones breves y la lluvia Matrix aparece desde sus primeros cuadros. Los cambios de pestaña, la finalización de una carpeta y la apertura de un pendiente también tienen transiciones discretas; seleccionar o desmarcar una carpeta no repite su animación de entrada. No se añaden esperas artificiales.

El cursor deja una estela de números Matrix con hasta 84 partículas. Conserva el puntero del sistema y no intercepta clics. Al detener el movimiento, libera el lienzo; se suspende al ocultar la pestaña y respeta movimiento reducido y dispositivos sin ratón. No utiliza recursos externos.

Magic PDF sigue disponible para compresión, división, unión, extracción y orden, rotación, imágenes a PDF y PDF a PNG. Su selección es independiente de la selección del expediente.

## Inicio y entorno

`INICIAR_MAGICZIP.bat` valida Python y Pillow/pypdf, inicia `app.py` y abre el navegador en `127.0.0.1`. La terminal debe permanecer abierta; `Ctrl+C` detiene el motor. La entrega no incluye ejecutables compilados.

El motor escucha únicamente en 127.0.0.1. No hay recursos CDN ni cargas a servidores de conversión. Se usan Python, pypdf, Pillow, PDFium y los programas Office instalados. Los helpers de Office solo cierran sus propias instancias identificadas; el original se lee a través de una copia privada.

Esta carpeta está dentro de OneDrive: la aplicación procesa localmente, pero Windows puede sincronizar los archivos según tu configuración. Para evitarlo, elige ubicaciones no sincronizadas para entrada y salida.

La entrega sin ejecutable requiere Python 3.11 o posterior, las dependencias de `requirements.txt` y esta carpeta del proyecto. No requiere administrador si Python y el entorno se instalan en el perfil del usuario. Está orientada a Windows 10/11 y utiliza el navegador del equipo. Las conversiones nativas siguen requiriendo Office; Office no se distribuye con el programa.

El portable contiene Python 3.11.9, Pillow 12.3.0, pypdf 6.10.0 y PDFium/pypdfium2 5.14.0. Utiliza su motor incluido aunque el equipo tenga Python 3.13, otra versión o ninguna instalación. El selector de carpetas también está incluido. Cada instalación identifica su propio motor local para evitar confundir la versión de desarrollo con una copia portable.

Los temporales de sesión permanecen en `work` junto al portable y se limpian con Nuevo ZIP o al cerrar. Office usa adicionalmente una carpeta temporal privada corta de Windows para evitar su límite de rutas, y la elimina al terminar la conversión. Los originales y los resultados finales permanecen en las ubicaciones elegidas por el usuario.

## Reconstruir el portable

La compilación se hace en Windows x64 con un entorno separado del entorno de desarrollo. Los paquetes PDF ya están en `vendor`.

```powershell
python -m venv work\portable-build\venv
.\work\portable-build\venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\work\portable-build\venv\Scripts\python.exe -I scripts\build_portable.py --zip
```

La salida queda en `dist/MagicZip_2.0_Portable_Win64` y su ZIP. El constructor no sobrescribe entregas existentes; acepta `--destination` para elegir otra carpeta dentro del proyecto. Incluye una verificación de PDF, imágenes, integridad ZIP y selector Tk desde el ejecutable congelado antes de publicar el paquete. El manifiesto de SHA-256 y las versiones se guardan dentro de `engine`.

## Desarrollo y validación

La selección nativa se ejecuta en una tarea cancelable. Al recibir la ruta, la pantalla cambia a lectura de archivos y muestra cantidades y bytes enumerados. El inventario utiliza solamente nombres, extensiones y metadatos del sistema: no calcula SHA-256, no abre PDF ni renderiza páginas. La ruta y el resultado se publican de forma atómica; cancelar conserva la selección anterior.

Al procesar se verifican únicamente los documentos seleccionados, con hasta dos lectores concurrentes. Los PDF bajo el límite reciben comprobaciones básicas y validación de apertura/renderizado, sin extraer anticipadamente texto o analizar todas sus imágenes. Los documentos que lo necesitan pasan al optimizador adaptativo existente. Se reutilizan inspecciones, validaciones y copias optimizadas dentro de la sesión mediante cachés limitadas. Se comprueba tamaño/fecha del original y se siguen verificando SHA-256 del original, de las copias y de cada entrada ZIP. Si una copia de caché cambia, se descarta. Nuevo ZIP elimina estas cachés.

La cancelación de lectura/análisis es cooperativa entre archivos, bloques y páginas; una llamada de biblioteca o de disco ya iniciada debe devolver el control antes de liberar su hilo. El selector nativo es un proceso propio y se termina al cancelar. Cerrar la sesión también cancela el inventario y espera a que finalice antes de limpiar temporales.

Benchmark reproducible con archivos sintéticos (5, 50 y 500 entradas y PDF de 20–50 MB):

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_pipeline.py --label after --jobs
```

Las evidencias están en `work/performance-audit`: referencia de fuentes con SHA-256 previa a las correcciones (esta carpeta no tiene Git), `before.json`, `after.json`, métricas de CPU/memoria, tiempos por etapa y pruebas de interfaz. El tiempo empleado por una persona dentro del selector de Windows se excluye del benchmark. Las cifras cortas de CPU tienen la granularidad del contador de Windows. No son garantías para almacenamiento remoto, archivos de OneDrive sin descargar o documentos de complejidad diferente.

La entrega actualizada se compila sin sobrescribir el portable anterior en `dist/MagicZip_2.0_Performance_Win64`. Su autodiagnóstico verifica también inventario sin análisis anticipado y procesamiento en segundo plano desde el motor congelado. Una instancia que ya estaba abierta debe reiniciarse para cargar cambios de código.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check web/app.js
node --check web/experience.js
```

El conjunto usa exclusivamente datos sintéticos y comprueba cortes, conservación de páginas, límites reales, firmas/cifrado, integridad, Excel sin pérdida, destino obligatorio, exclusión de ZIPS FINALES, informes y API local. Las pruebas nativas adicionales se ejecutan con tests/create_office_fixtures.ps1 y scripts/check_office.py; no usan documentos personales.

Los artefactos de prueba quedan en work. El límite de edición PDF/carga individual desde navegador sigue siendo 512 MiB; para ZIP de archivos mayores utiliza selección de carpeta nativa. Reconstrucción del acceso: scripts/build_launcher.ps1.
