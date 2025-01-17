from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, create_model, HttpUrl
from typing import Literal, Any, Callable
import lumos
from functools import wraps
import tempfile
import os
import requests
from fastapi import UploadFile, File
from ..book.parser import from_pdf_path

app = FastAPI(title="Lumos API")


def require_api_key(func: Callable):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        request: Request = kwargs.get("request") or args[0]
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            raise HTTPException(status_code=401, detail="API key is missing")
        if not api_key.strip():
            raise HTTPException(status_code=401, detail="Invalid API key")
        if api_key != "12345678":
            raise HTTPException(status_code=401, detail="Invalid API key")
        return await func(*args, **kwargs)

    return wrapper


class ChatMessage(BaseModel):
    role: str = Literal["system", "user", "assistant", "developer"]
    content: str


class AIRequest(BaseModel):
    messages: list[ChatMessage]
    response_schema: dict[str, Any] | None  # JSONschema
    examples: list[tuple[str, dict[str, Any]]] | None = None
    model: str | None = "gpt-4o-mini"


class EmbedRequest(BaseModel):
    inputs: str | list[str]
    model: str | None = "text-embedding-3-small"


class PDFRequest(BaseModel):
    url: HttpUrl | None = None


def _json_schema_to_pydantic_types(
    schema: dict[str, Any],
) -> dict[str, tuple[type, Any]]:
    """Convert JSON schema types to Python/Pydantic types"""
    type_mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    field_types = {}
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for field_name, field_schema in properties.items():
        python_type = type_mapping[field_schema["type"]]
        # If field is required, use ... as default, otherwise None
        default = ... if field_name in required else None
        field_types[field_name] = (python_type, default)

    return field_types


@app.post("/generate")
@require_api_key
async def create_chat_completion(request: Request, ai_request: AIRequest):
    """
    Examples can only be used if response_schema is provided, and are in json format
    """
    try:
        ResponseModel = None
        formatted_examples = None

        # Convert JSON schema to Pydantic field types
        if ai_request.response_schema:
            field_types = _json_schema_to_pydantic_types(ai_request.response_schema)
            ResponseModel = create_model("DynamicResponseModel", **field_types)

        if ai_request.examples:
            formatted_examples = [
                (query, ResponseModel.model_validate(response))
                for query, response in ai_request.examples
            ]

        # Convert messages to dict format
        messages = [msg.model_dump() for msg in ai_request.messages]

        # Call the AI function
        result = await lumos.call_ai_async(
            messages=messages,
            response_format=ResponseModel,
            examples=formatted_examples,
            model=ai_request.model,
        )

        return result.model_dump()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/embed")
@require_api_key
async def embed(request: Request, embed_request: EmbedRequest):
    return lumos.get_embedding(embed_request.inputs, embed_request.model)


@app.get("/healthz")
@require_api_key
async def health_check(request: Request):
    return {"status": "healthy"}


@app.get("/")
async def root(request: Request):
    return {"message": "Lumos API"}


@app.post("/book/parse-pdf")
@require_api_key
async def process_pdf(
    request: Request,
    pdf_request: PDFRequest | None = None,
    file: UploadFile | None = File(None),
):
    """Process a PDF file from either a URL or uploaded file."""
    if not pdf_request and not file:
        raise HTTPException(
            status_code=400, detail="Either a PDF URL or file upload must be provided"
        )

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            if pdf_request and pdf_request.url:
                # Download from URL
                response = requests.get(pdf_request.url)
                response.raise_for_status()
                tmp_file.write(response.content)
            elif file:
                # Handle uploaded file
                content = await file.read()
                tmp_file.write(content)

            tmp_file.flush()

            # Process the PDF
            try:
                book = from_pdf_path(tmp_file.name)
                sections = book.flatten_sections(only_leaf=True)
                raw_chunks = book.flatten_chunks()

                return {"sections": sections, "chunks": raw_chunks}
            finally:
                # Clean up temp file
                os.unlink(tmp_file.name)

    except requests.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Failed to download PDF: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process PDF: {str(e)}")
