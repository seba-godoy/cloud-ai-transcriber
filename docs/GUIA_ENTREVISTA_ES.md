# Entender y presentar Cloud AI Transcriber

Esta guía explica el caso de uso cloud presentado en el portafolio. El repositorio público es una copia saneada del proyecto; no es un servicio público ni un inventario verificado del despliegue privado actual.

## El problema y el resultado

El proyecto nace de una necesidad personal: convertir grabaciones largas de clases en documentos que sea más fácil consultar y organizar. El resultado esperado es recibir una grabación, transcribirla con IA, construir documentos y guardarlos en Drive, con información del progreso por Telegram.

Para una demostración breve, mostrar una grabación de ejemplo, el esquema del flujo y un documento ya generado. Utilizar contenido ficticio o autorizado, sin credenciales ni grabaciones de terceros. Una transcripción larga no necesita ejecutarse en vivo durante la entrevista.

## Qué hace cada parte

| Parte | Responsabilidad | Distinción útil |
| --- | --- | --- |
| Python | Coordina las etapas y maneja errores | Es el programa que conecta los servicios |
| Cloud Run Job | Ejecuta el programa en un contenedor | No necesita un computador personal encendido |
| Cloud Scheduler | Inicia ejecuciones programadas | No significa que el programa esté corriendo continuamente |
| FFmpeg / FFprobe | Inspeccionan y preparan el audio | No son el modelo de transcripción |
| Gemini API | Recibe fragmentos y genera texto | El proyecto utiliza un modelo existente; no entrena uno propio |
| Google Drive | Conserva grabaciones y documentos | No es la base de datos del progreso del bot |
| Firestore | Guarda estado, resultados parciales y coordinación | Permite recordar qué trabajo se completó |
| Telegram | Entrada autorizada, comandos y avisos | Un aviso fallido no equivale necesariamente a una transcripción fallida |
| Secret Manager e IAM | Gestionan secretos y permisos cloud | La autorización OAuth de Drive es un mecanismo separado |
| GitHub Actions | Ejecuta comprobaciones automatizadas | CI aprobado no prueba por sí solo la precisión de una transcripción |

## Recorrer una grabación

1. Una ejecución busca entradas y verifica su identidad para reducir duplicados.
2. El sistema coordina la propiedad del trabajo para reducir ejecuciones simultáneas sobre lo mismo.
3. Inspecciona el audio y lo procesa en fragmentos acotados.
4. Llama a Gemini para transcribir y guarda resultados parciales.
5. Si se interrumpe, los checkpoints permiten reutilizar trabajo ya persistido.
6. Ensambla los documentos y los publica en Drive con mecanismos contra subidas duplicadas.
7. Registra el estado y envía información por Telegram.

Las garantías dependen de la ruta y configuración. Estos mecanismos reducen fallos; no prometen que nunca habrá errores o duplicados.

## Conceptos para explicar con un ejemplo

- **Checkpoint:** un punto guardado de progreso. Si una grabación tiene muchos fragmentos y el proceso se corta, permite recuperar los ya completados.
- **Idempotencia:** repetir una operación sin multiplicar su efecto. Identidades estables e IDs de salida ayudan a evitar documentos duplicados al reintentar.
- **Lease:** permiso temporal para procesar un trabajo. Ayuda a coordinar ejecuciones que podrían solaparse; no es una promesa de exclusión perfecta bajo cualquier fallo.
- **Retry y fallback:** reintentar ante un fallo transitorio o utilizar una alternativa configurada. Ninguno garantiza exactitud del texto.
- **Contenedor:** paquete reproducible con el programa y sus dependencias para ejecutarlo en un entorno compatible.
- **OAuth e IAM:** OAuth autoriza acceso a recursos de un usuario, como Drive; IAM controla lo que una identidad de servicio puede hacer dentro de Google Cloud.

## Cómo describir la contribución personal

Separar la definición del problema, los requisitos, la coordinación del desarrollo asistido por IA, las pruebas con grabaciones y la investigación de fallos. La IA ayudó a implementar código. No atribuirse programación manual de cada línea ni entrenamiento del modelo.

Preparar un incidente real que se pueda explicar: qué ocurrió, qué evidencia se revisó, qué se cambió y cómo se verificó. No inventar métricas de ahorro, precisión, uso o costos. Si una cifra no está medida y vigente, presentarla como pendiente de medir.

## Preguntas que conviene poder responder

- ¿Por qué no bastaba con subir un audio manualmente a una IA?
- ¿Qué ocurre si se corta el proceso o se repite una entrada?
- ¿Dónde están los archivos y dónde está el progreso?
- ¿Cómo restringes el acceso y evitas publicar credenciales?
- ¿Qué limitaciones tiene la transcripción y cómo evaluarías su calidad?
- ¿Qué parte hiciste tú y en qué te ayudó la IA?
- ¿Qué simplificarías si otra persona tuviera que mantenerlo?

## Conexión con un asistente institucional

Los aprendizajes transferibles son ordenar fuentes, definir el resultado esperado, controlar accesos, probar casos reales y documentar operación y recuperación. Un asistente de consultas es otro caso de uso: necesita descubrir usuarios, canales, fuentes autorizadas y responsables de actualización antes de escoger una plataforma.

## Lecturas del repositorio

- [Presentación y esquema](../README.md)
- [Arquitectura](ARCHITECTURE.md)
- [Seguridad y privacidad](SECURITY_MODEL.md)

El esquema representa el flujo cloud presentado. El snapshot puede conservar adaptadores históricos que no forman parte de ese caso de uso.
