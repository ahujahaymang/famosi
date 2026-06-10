"""Pydantic extraction schemas for doctor question logging."""

from pydantic import BaseModel


class DoctorQuestionExtraction(BaseModel):
    """A question to ask the doctor, extracted from a user message."""

    question_text: str
