"""
Source configurations for the knowledge ingestion pipeline.

Defines the curated list of trusted medical and public-health sources
used to populate the ``knowledge_documents`` / ``knowledge_chunks`` tables.

Each entry is a ``SourceConfig`` dataclass that specifies:
  - ``name``     — short identifier used as the ``source`` column value
                   (matches the design constraint: VARCHAR(20))
  - ``category`` — ``KnowledgeCategory`` enum value
  - ``urls``     — list of URLs to fetch during ingestion

Sources covered (Req 15.1, 15.3):
  - ACOG  (American College of Obstetricians and Gynecologists)
  - WHO   (World Health Organization)
  - CDC   (Centers for Disease Control and Prevention)
  - NHS   (National Health Service, UK)

Requirements: 15.1, 15.3
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.knowledge_document import KnowledgeCategory


@dataclass(frozen=True)
class SourceConfig:
    """Immutable configuration for a single knowledge ingestion source."""

    name: str
    """Short publisher identifier stored in ``knowledge_documents.source``."""

    category: KnowledgeCategory
    """Knowledge category this source primarily covers."""

    urls: list[str] = field(default_factory=list)
    """Public URLs to fetch raw text from during ingestion."""


# ---------------------------------------------------------------------------
# ACOG — American College of Obstetricians and Gynecologists
# ---------------------------------------------------------------------------

ACOG_SOURCES: list[SourceConfig] = [
    SourceConfig(
        name="ACOG",
        category=KnowledgeCategory.nutrition,
        urls=[
            "https://www.acog.org/womens-health/faqs/nutrition-during-pregnancy",
        ],
    ),
    SourceConfig(
        name="ACOG",
        category=KnowledgeCategory.exercise,
        urls=[
            "https://www.acog.org/womens-health/faqs/exercise-during-pregnancy",
        ],
    ),
    SourceConfig(
        name="ACOG",
        category=KnowledgeCategory.medications,
        urls=[
            "https://www.acog.org/womens-health/faqs/medications-and-pregnancy",
        ],
    ),
    SourceConfig(
        name="ACOG",
        category=KnowledgeCategory.labor,
        urls=[
            "https://www.acog.org/womens-health/faqs/labor-induction",
            "https://www.acog.org/womens-health/faqs/stages-of-labor",
        ],
    ),
    SourceConfig(
        name="ACOG",
        category=KnowledgeCategory.postpartum,
        urls=[
            "https://www.acog.org/womens-health/faqs/postpartum-depression",
        ],
    ),
    SourceConfig(
        name="ACOG",
        category=KnowledgeCategory.mental_health,
        urls=[
            "https://www.acog.org/womens-health/faqs/perinatal-depression",
        ],
    ),
]

# ---------------------------------------------------------------------------
# WHO — World Health Organization
# ---------------------------------------------------------------------------

WHO_SOURCES: list[SourceConfig] = [
    SourceConfig(
        name="WHO",
        category=KnowledgeCategory.nutrition,
        urls=[
            "https://www.who.int/news-room/fact-sheets/detail/malnutrition",
            "https://www.who.int/tools/elena/interventions/salt-reduction-pregnancy",
        ],
    ),
    SourceConfig(
        name="WHO",
        category=KnowledgeCategory.symptoms,
        urls=[
            "https://www.who.int/news-room/fact-sheets/detail/preeclampsia",
        ],
    ),
    SourceConfig(
        name="WHO",
        category=KnowledgeCategory.baby_development,
        urls=[
            "https://www.who.int/news-room/fact-sheets/detail/preterm-birth",
        ],
    ),
    SourceConfig(
        name="WHO",
        category=KnowledgeCategory.mental_health,
        urls=[
            "https://www.who.int/news-room/fact-sheets/detail/mental-disorders",
        ],
    ),
    SourceConfig(
        name="WHO",
        category=KnowledgeCategory.postpartum,
        urls=[
            "https://www.who.int/news-room/fact-sheets/detail/maternal-mortality",
        ],
    ),
]

# ---------------------------------------------------------------------------
# CDC — Centers for Disease Control and Prevention
# ---------------------------------------------------------------------------

CDC_SOURCES: list[SourceConfig] = [
    SourceConfig(
        name="CDC",
        category=KnowledgeCategory.nutrition,
        urls=[
            "https://www.cdc.gov/nutrition/pregnancy-breastfeeding/index.html",
            "https://www.cdc.gov/folic-acid/about/index.html",
        ],
    ),
    SourceConfig(
        name="CDC",
        category=KnowledgeCategory.symptoms,
        urls=[
            "https://www.cdc.gov/maternal-infant-health/pregnancy-complications/index.html",
        ],
    ),
    SourceConfig(
        name="CDC",
        category=KnowledgeCategory.medications,
        urls=[
            "https://www.cdc.gov/medication-safety/data-research/pregnancy.html",
        ],
    ),
    SourceConfig(
        name="CDC",
        category=KnowledgeCategory.baby_development,
        urls=[
            "https://www.cdc.gov/ncbddd/pregnancy_gateway/index.html",
        ],
    ),
    SourceConfig(
        name="CDC",
        category=KnowledgeCategory.mental_health,
        urls=[
            "https://www.cdc.gov/maternal-infant-health/depression/index.html",
        ],
    ),
    SourceConfig(
        name="CDC",
        category=KnowledgeCategory.dad_support,
        urls=[
            "https://www.cdc.gov/ncbddd/pregnancy_gateway/partners.html",
        ],
    ),
]

# ---------------------------------------------------------------------------
# NHS — National Health Service (UK)
# ---------------------------------------------------------------------------

NHS_SOURCES: list[SourceConfig] = [
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.nutrition,
        urls=[
            "https://www.nhs.uk/pregnancy/keeping-well/have-a-healthy-diet/",
            "https://www.nhs.uk/pregnancy/keeping-well/foods-to-avoid/",
        ],
    ),
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.symptoms,
        urls=[
            "https://www.nhs.uk/pregnancy/related-conditions/common-symptoms/",
            "https://www.nhs.uk/pregnancy/related-conditions/complications/",
        ],
    ),
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.exercise,
        urls=[
            "https://www.nhs.uk/pregnancy/keeping-well/exercise/",
        ],
    ),
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.medications,
        urls=[
            "https://www.nhs.uk/pregnancy/keeping-well/medicines/",
        ],
    ),
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.baby_development,
        urls=[
            "https://www.nhs.uk/pregnancy/week-by-week/",
        ],
    ),
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.labor,
        urls=[
            "https://www.nhs.uk/pregnancy/labour-and-birth/signs-of-labour/",
            "https://www.nhs.uk/pregnancy/labour-and-birth/what-happens/",
        ],
    ),
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.postpartum,
        urls=[
            "https://www.nhs.uk/conditions/baby/support-and-services/your-body-after-the-birth/",
        ],
    ),
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.mental_health,
        urls=[
            "https://www.nhs.uk/mental-health/conditions/post-natal-depression/overview/",
        ],
    ),
    SourceConfig(
        name="NHS",
        category=KnowledgeCategory.dad_support,
        urls=[
            "https://www.nhs.uk/pregnancy/support-and-services/maternity-support-workers/",
        ],
    ),
]

# ---------------------------------------------------------------------------
# Aggregated list of all sources — used by the ingestion pipeline
# ---------------------------------------------------------------------------

ALL_SOURCES: list[SourceConfig] = (
    ACOG_SOURCES + WHO_SOURCES + CDC_SOURCES + NHS_SOURCES
)
