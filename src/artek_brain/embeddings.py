"""Эмбеддеры для опционального семантического слоя (только stdlib).

В ядре нет обязательной модели: `HashEmbedder` детерминирован и работает без
сети, а `OllamaEmbedder` обращается к локальному Ollama по HTTP. Ошибки сети
превращаются в `EmbeddingError` с понятной подсказкой.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from typing import Protocol, Sequence, runtime_checkable

from .errors import ArtekBrainError, ValidationError

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_HASH_DIM = 256

_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)


class EmbeddingError(ArtekBrainError):
    """Не удалось получить эмбеддинги (сеть, ответ модели или настройки)."""


@runtime_checkable
class Embedder(Protocol):
    """Минимальный интерфейс эмбеддера."""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Вернуть по вектору на каждый текст."""
        ...


def _as_texts(texts: Sequence[str]) -> list[str]:
    if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
        raise ValidationError(
            "embed принимает список строк.",
            hint="Передайте list[str], например embed([\"текст\"]).",
        )
    result: list[str] = []
    for item in texts:
        if not isinstance(item, str):
            raise ValidationError(
                "Каждый текст для embed должен быть строкой.",
                hint="Приведите элементы списка к str.",
            )
        result.append(item)
    return result


def _hash_tokens(text: str) -> list[tuple[str, float]]:
    """Слова и символьные триграммы слов с весами."""
    words = _WORD_RE.findall(text.lower())
    tokens: list[tuple[str, float]] = [(word, 1.0) for word in words]
    for word in words:
        padded = f" {word} "
        if len(padded) < 3:
            continue
        for index in range(len(padded) - 2):
            tokens.append(("~" + padded[index : index + 3], 0.5))
    return tokens


class HashEmbedder:
    """Детерминированный эмбеддер на hashing trick (слова и триграммы)."""

    def __init__(self, dim: int = DEFAULT_HASH_DIM) -> None:
        if not isinstance(dim, int) or isinstance(dim, bool) or dim < 8:
            raise ValidationError(
                "dim эмбеддера должен быть целым числом не меньше 8.",
                hint="Передайте, например, dim=256.",
            )
        self.dim = int(dim)
        self.name = f"hash:{self.dim}"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token, weight in _hash_tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Построить нормированные векторы без сети и модели."""
        return [self._vector(text) for text in _as_texts(texts)]


class OllamaEmbedder:
    """Клиент локального Ollama (`POST /api/embed`) на urllib."""

    def __init__(
        self,
        model: str,
        url: str = DEFAULT_OLLAMA_URL,
        *,
        timeout: float = 10.0,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise EmbeddingError(
                "Не указана модель Ollama.",
                hint="Задайте model в настройках, например nomic-embed-text.",
            )
        if not isinstance(url, str) or not url.strip():
            raise EmbeddingError(
                "Не указан адрес Ollama.",
                hint=f"Укажите url, по умолчанию {DEFAULT_OLLAMA_URL}.",
            )
        self.model = model.strip()
        self.url = url.rstrip("/")
        self.timeout = float(timeout)
        self.name = f"ollama:{self.model}"
        self.dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Получить векторы у локального Ollama; при сбое — EmbeddingError."""
        items = _as_texts(texts)
        if not items:
            return []
        payload = json.dumps(
            {"model": self.model, "input": items}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EmbeddingError(
                f"Не удалось получить эмбеддинги от Ollama по адресу {self.url}.",
                hint=(
                    "Проверьте, что Ollama запущена (ollama serve) и модель "
                    f"«{self.model}» загружена (ollama pull {self.model})."
                ),
            ) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EmbeddingError(
                "Ollama вернула ответ, который не является JSON.",
                hint="Проверьте версию Ollama и модель эмбеддингов.",
            ) from exc
        embeddings = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != len(items):
            raise EmbeddingError(
                "Ollama вернула неверное число векторов.",
                hint="Ожидается ответ вида {\"embeddings\": [[...], ...]}.",
            )
        result: list[list[float]] = []
        dim: int | None = None
        for vector in embeddings:
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError(
                    "Ollama вернула пустой вектор.",
                    hint="Проверьте, что модель поддерживает эмбеддинги.",
                )
            if not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in vector
            ):
                raise EmbeddingError(
                    "Ollama вернула вектор с нечисловыми значениями.",
                    hint="Проверьте модель и версию Ollama.",
                )
            if dim is None:
                dim = len(vector)
            elif len(vector) != dim:
                raise EmbeddingError(
                    "Ollama вернула векторы разной размерности.",
                    hint="Проверьте модель эмбеддингов.",
                )
            result.append([float(value) for value in vector])
        if dim is not None:
            if self.dim and self.dim != dim:
                raise EmbeddingError(
                    f"Размерность векторов Ollama изменилась: {self.dim} → {dim}.",
                    hint="Перестройте семантический индекс (reindex).",
                )
            self.dim = dim
        return result


def build_embedder(
    kind: str | None,
    *,
    dim: int = DEFAULT_HASH_DIM,
    model: str | None = None,
    url: str = DEFAULT_OLLAMA_URL,
    timeout: float = 10.0,
) -> Embedder:
    """Создать эмбеддер по настройкам или вернуть понятную ошибку."""
    value = (kind or "").strip().lower()
    if value == "hash":
        return HashEmbedder(dim)
    if value == "ollama":
        if not model:
            raise EmbeddingError(
                "Для Ollama не указана модель эмбеддингов.",
                hint="Задайте model, например nomic-embed-text.",
            )
        return OllamaEmbedder(model, url=url, timeout=timeout)
    raise EmbeddingError(
        f"Неизвестный тип эмбеддера «{kind}».",
        hint="Допустимо: hash или ollama.",
    )
