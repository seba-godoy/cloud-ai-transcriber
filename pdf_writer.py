import os
from fpdf import FPDF
from config import Config
from utils import log_error, sanitize_filename, redact_secrets

LINUX_FONT_PAIRS = (
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
)


def resolve_pdf_fonts(regular_override=None, bold_override=None):
    """Return existing Unicode TrueType fonts, honoring configured overrides first."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    regular_candidates = [regular_override]
    bold_candidates = [bold_override]
    for regular, bold in LINUX_FONT_PAIRS:
        regular_candidates.append(regular)
        bold_candidates.append(bold)
    regular_candidates.extend(os.path.join(windir, "Fonts", name) for name in
                              ("segoeui.ttf", "arial.ttf", "tahoma.ttf"))
    bold_candidates.extend(os.path.join(windir, "Fonts", name) for name in
                           ("segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf"))

    chosen_regular = next((path for path in regular_candidates if path and os.path.isfile(path)), None)
    chosen_bold = next((path for path in bold_candidates if path and os.path.isfile(path)), None)
    if not chosen_regular:
        raise RuntimeError(
            "No valid TrueType Unicode font found for PDF generation. "
            "Set PDF_FONT_REGULAR/PDF_FONT_BOLD to readable .ttf files."
        )
    return chosen_regular, chosen_bold

def create_pdf(transcript: str, original_file_name: str) -> str:
    """
    Creates a PDF file containing the transcript using fpdf2 and TrueType Unicode fonts.
    Raises RuntimeError if no valid TTF font is found.
    """
    os.makedirs(Config.OUTPUT_DIR, exist_ok=True)
    
    clean_original_name = sanitize_filename(original_file_name)
    base_name = os.path.splitext(clean_original_name)[0]
    pdf_filename = f"{base_name}.pdf"
    pdf_path = os.path.join(Config.OUTPUT_DIR, pdf_filename)
    
    chosen_regular, chosen_bold = resolve_pdf_fonts(
        Config.PDF_FONT_REGULAR, Config.PDF_FONT_BOLD
    )

    try:
        pdf = FPDF()
        pdf.add_page()
        
        pdf.add_font("UnicodeFont", style="", fname=chosen_regular)
        font_name = "UnicodeFont"
        if chosen_bold:
            pdf.add_font("UnicodeFont", style="B", fname=chosen_bold)
            
        pdf.set_font(font_name, style="B" if chosen_bold else "", size=16)
        title_text = f"Transcript: {clean_original_name}"
        pdf.cell(0, 10, title_text, new_x="LMARGIN", new_y="NEXT", align='C')
        pdf.ln(10)
        
        pdf.set_font(font_name, style="", size=12)
        pdf.multi_cell(0, 10, transcript)
        
        pdf.output(pdf_path)
        return pdf_path
    except Exception as e:
        log_error("create_pdf", redact_secrets(str(e)))
        raise
