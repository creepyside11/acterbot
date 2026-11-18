import base64
import csv
import io
import warnings
import zipfile
from pathlib import Path

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 15_000_000
TEXT_TYPES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html",
    ".css", ".js", ".py", ".lua", ".sql", ".ini", ".log", ".toml", ".svg", ".rs", ".go",
}
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
OUTPUT_TYPES = TEXT_TYPES - {".svg"} | {".xlsx"}
FILE_TEXT_LIMIT = 18_000


def image_part(data):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1400, 1400))
            output = io.BytesIO()
            if "A" in image.getbands():
                background = Image.new("RGBA", image.size, "white")
                background.alpha_composite(image.convert("RGBA"))
                image = background
            image.convert("RGB").save(output, "JPEG", quality=83)
    url = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()
    return {"type": "image_url", "image_url": {"url": url}}


def check_office(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        info = zipped.infolist()
        if len(info) > 1200 or sum(item.file_size for item in info) > 24 * 1024 * 1024:
            raise ValueError("Документ слишком большой после распаковки")


def read_text(name, data):
    ext = Path(name).suffix.lower()
    if ext in TEXT_TYPES:
        try:
            result = data[:FILE_TEXT_LIMIT * 4].decode("utf-8-sig")
        except UnicodeDecodeError:
            result = data[:FILE_TEXT_LIMIT * 4].decode("cp1251", errors="replace")
    elif ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts = []
        pages = min(15, len(reader.pages))
        for i in range(pages):
            page = reader.pages[i]
            contents = page.get_contents()
            if contents and len(contents.get_data()) > 5 * 1024 * 1024:
                raise ValueError("Слишком большой текстовый поток PDF")
            parts.append(f"[Страница {i + 1}]\n" + (page.extract_text() or ""))
            if sum(map(len, parts)) > FILE_TEXT_LIMIT:
                break
        result = "\n".join(parts)
        if len(reader.pages) > pages:
            result += f"\n[Показаны первые {pages} страниц из {len(reader.pages)}]"
        if all(part.split("\n", 1)[-1].strip() == "" for part in parts):
            result = "В PDF не найден текстовый слой. Прикрепите скриншоты страниц для анализа."
    elif ext == ".docx":
        from docx import Document

        check_office(data)
        document = Document(io.BytesIO(data))
        parts = [paragraph.text for paragraph in document.paragraphs[:500]]
        for table in document.tables[:8]:
            for row in table.rows[:40]:
                parts.append(" | ".join(cell.text[:300] for cell in row.cells[:16]))
        result = "\n".join(parts)
    elif ext == ".xlsx":
        from openpyxl import load_workbook

        check_office(data)
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        try:
            parts = []
            for sheet in workbook.worksheets[:4]:
                parts.append(f"[{sheet.title}]")
                for row in sheet.iter_rows(max_row=80, max_col=16, values_only=True):
                    if any(value is not None for value in row):
                        parts.append(" | ".join(str(value)[:250] if value is not None else "" for value in row))
            result = "\n".join(parts)
        finally:
            workbook.close()
    else:
        raise ValueError("Формат файла не поддерживается для чтения")
    return result[:FILE_TEXT_LIMIT] + ("\n[Текст сокращён]" if len(result) > FILE_TEXT_LIMIT else "")


def create_file(name, content):
    if len(content) > 250_000:
        raise ValueError("Файл результата слишком большой")
    ext = Path(name).suffix.lower()
    if ext not in OUTPUT_TYPES:
        raise ValueError("Выберите текстовое расширение или .xlsx")
    if ext != ".xlsx":
        return content.encode()
    from openpyxl import Workbook

    book = Workbook(write_only=True)
    sheet = book.create_sheet("Результат")
    for index, row in enumerate(csv.reader(io.StringIO(content))):
        if index >= 1000 or len(row) > 60:
            raise ValueError("До 1000 строк и 60 столбцов")
    # Validate the dimensions before adding rows to the temporary workbook.
    for row in csv.reader(io.StringIO(content)):
        from openpyxl.cell import WriteOnlyCell

        cells = []
        for value in row:
            cell = WriteOnlyCell(sheet, value=value)
            cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
