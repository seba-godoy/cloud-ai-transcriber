import os
from google import genai
from config import Config
from utils import log_error, log_ok, log_start, retry, redact_secrets, is_transient_error

@retry(attempts=3, delay=5)
def generate_summary(transcript: str, title: str, model: str = None) -> str:
    """
    Generates a structured summary using Gemini Pro directly from transcript text.
    """
    if not transcript or not transcript.strip():
        raise Exception("Empty transcript provided to summarizer.")
        
    model = model or Config.GEMINI_FALLBACK_MODEL
    client = genai.Client(api_key=Config.GEMINI_API_KEY)
    
    prompt = f"""Escribe un resumen estructurado del siguiente video llamado "{title}".

Reglas estrictas:
1. Máximo 5 puntos clave, mínimo 3.
2. Cada punto clave debe ser una oración completa y específica, no un título vago. Ejemplo correcto: "La estrategia de precios dinámicos permite aumentar el margen en un 15% sin perder volumen de ventas." Ejemplo incorrecto: "Precios dinámicos."
3. La idea principal debe responder: ¿de qué trata este video en una sola idea?
4. La conclusión debe responder: ¿qué debería hacer o recordar el espectador después de ver esto?
5. El resumen debe estar en el mismo idioma que el video.
6. Si el video mezcla español e inglés, el resumen debe ser en español.
7. No incluir timestamps ni referencias a partes específicas del video.
8. No incluir opiniones propias, solo sintetizar lo que dice el video.
9. Usa encabezados en texto plano sin asteriscos de formato Markdown.

Debes devolver EXACTAMENTE este formato:

📋 Resumen: {title}

🎯 Idea principal:
[Tu idea principal aquí]

📌 Puntos clave:
• [Punto clave 1]
• [Punto clave 2]
• [Punto clave 3]
[Más puntos si es necesario, hasta 5]

💡 Conclusión:
[Tu conclusión aquí]

📄 Transcript completo disponible en Drive


A continuación el transcript completo del video:
---
{transcript}
---
"""
    
    log_start(f"Generating summary for '{title}' using {model}")
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt
        )
        text = response.text.strip() if response and response.text else ""
        if not text:
            raise Exception("Summary generation returned empty text.")
        return text
    except Exception as e:
        log_error("generate_summary", redact_secrets(str(e)))
        raise
