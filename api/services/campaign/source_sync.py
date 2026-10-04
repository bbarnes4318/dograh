from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import re

from loguru import logger

from api.utils.telephony_address import is_dialable_pstn

PHONE_COLUMN = "phone_number"

# Header spellings (after lowercasing and collapsing separators to "_") that are
# treated as the phone column when no literal ``phone_number`` header exists.
# Spreadsheets exported from CRMs / lead vendors rarely use our exact name.
PHONE_COLUMN_ALIASES = (
    "phone",
    "phonenumber",
    "phone_no",
    "phone_num",
    "phone_#",
    "primary_phone",
    "mobile",
    "mobile_number",
    "mobile_phone",
    "cell",
    "cell_phone",
    "cell_number",
    "telephone",
    "telephone_number",
    "tel",
    "number",
    "contact_number",
    "home_phone",
)

_HEADER_SEP_RE = re.compile(r"[\s\-\.]+")
_NON_DIGIT_RE = re.compile(r"\D")
_EXCEL_FLOAT_RE = re.compile(r"^\+?\d+\.0+$")


@dataclass
class ValidationError:
    """Represents a validation error with details."""

    message: str
    invalid_rows: Optional[List[int]] = None


@dataclass
class ValidationResult:
    """Result of source validation."""

    is_valid: bool
    error: Optional[ValidationError] = None
    headers: Optional[List[str]] = field(default=None, repr=False)
    rows: Optional[List[List[str]]] = field(default=None, repr=False)


class CampaignSourceSyncService(ABC):
    """Base class for campaign data source synchronization"""

    @staticmethod
    def normalize_headers(headers: List[str]) -> List[str]:
        """Normalize headers by stripping whitespace/BOM and lowercasing.

        If no ``phone_number`` header is present, the first header matching a
        known phone alias (``Phone``, ``Phone Number``, ``Mobile``...) is renamed
        to ``phone_number`` so the rest of the pipeline can find it.
        """
        normalized = [h.replace("\ufeff", "").strip().lower() for h in headers]
        if PHONE_COLUMN in normalized:
            return normalized

        collapsed = [_HEADER_SEP_RE.sub("_", h).strip("_") for h in normalized]
        if PHONE_COLUMN in collapsed:
            normalized[collapsed.index(PHONE_COLUMN)] = PHONE_COLUMN
            return normalized
        for alias in PHONE_COLUMN_ALIASES:
            if alias in collapsed:
                normalized[collapsed.index(alias)] = PHONE_COLUMN
                return normalized
        return normalized

    @staticmethod
    def normalize_phone_number(raw: str) -> str:
        """Best-effort conversion of a spreadsheet phone value to E.164.

        Bare 10-digit and 1-prefixed 11-digit numbers are assumed to be NANP
        (US/Canada) and get a ``+1`` prefix; formatting characters are removed.
        Anything else is returned stripped so validation can reject it.
        """
        value = (raw or "").strip()
        if not value or value.lower().startswith(("sip:", "sips:")):
            return value
        # Excel often turns 5551234567 into "5551234567.0"
        if _EXCEL_FLOAT_RE.match(value):
            value = value.split(".", 1)[0]

        digits = _NON_DIGIT_RE.sub("", value)
        if value.startswith("+"):
            return f"+{digits}" if digits else value
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        return value

    @staticmethod
    def normalize_rows(
        normalized_headers: List[str], rows: List[List[str]]
    ) -> List[List[str]]:
        """Return rows with the phone column normalized to E.164 where possible."""
        if PHONE_COLUMN not in normalized_headers:
            return rows
        idx = normalized_headers.index(PHONE_COLUMN)
        out = []
        for row in rows:
            if len(row) > idx:
                row = list(row)
                row[idx] = CampaignSourceSyncService.normalize_phone_number(row[idx])
            out.append(row)
        return out

    @staticmethod
    def validate_source_data(
        headers: List[str], rows: List[List[str]]
    ) -> ValidationResult:
        """
        Validate source data for campaign creation.

        Args:
            headers: List of column headers
            rows: List of data rows (excluding header)

        Returns:
            ValidationResult with is_valid=True if valid, or error details if invalid
        """
        normalized_headers = CampaignSourceSyncService.normalize_headers(headers)

        # Check for phone_number column
        if PHONE_COLUMN not in normalized_headers:
            found = ", ".join(f"'{h}'" for h in normalized_headers if h) or "none"
            return ValidationResult(
                is_valid=False,
                error=ValidationError(
                    message="Source must contain a 'phone_number' column (or a "
                    "column named 'phone', 'phone number', 'mobile', 'cell'). "
                    f"Columns found: {found}"
                ),
            )

        rows = CampaignSourceSyncService.normalize_rows(normalized_headers, rows)
        phone_number_idx = normalized_headers.index(PHONE_COLUMN)

        # Validate phone numbers in all data rows
        invalid_rows = []
        for row_idx, row in enumerate(
            rows, start=2
        ):  # Start at 2 (1-indexed, skip header)
            if len(row) <= phone_number_idx:
                continue  # Skip rows that don't have enough columns

            phone_number = row[phone_number_idx].strip()
            if phone_number and (
                not phone_number.startswith("+") or not is_dialable_pstn(phone_number)
            ):
                invalid_rows.append(row_idx)

        if invalid_rows:
            # Limit the number of rows shown in error message
            if len(invalid_rows) > 5:
                rows_str = f"{', '.join(map(str, invalid_rows[:5]))} and {len(invalid_rows) - 5} more"
            else:
                rows_str = ", ".join(map(str, invalid_rows))

            return ValidationResult(
                is_valid=False,
                error=ValidationError(
                    message=f"Invalid phone numbers in rows: {rows_str}. All phone numbers must include country code (start with '+') and be dialable",
                    invalid_rows=invalid_rows,
                ),
            )

        # Check for duplicate phone numbers
        seen_phones: dict[str, int] = {}  # phone -> first row where it appeared
        duplicate_rows = []
        for row_idx, row in enumerate(rows, start=2):
            if len(row) <= phone_number_idx:
                continue

            phone_number = row[phone_number_idx].strip()
            if not phone_number:
                continue

            if phone_number in seen_phones:
                duplicate_rows.append(row_idx)
            else:
                seen_phones[phone_number] = row_idx

        if duplicate_rows:
            if len(duplicate_rows) > 5:
                rows_str = f"{', '.join(map(str, duplicate_rows[:5]))} and {len(duplicate_rows) - 5} more"
            else:
                rows_str = ", ".join(map(str, duplicate_rows))

            return ValidationResult(
                is_valid=False,
                error=ValidationError(
                    message=f"Duplicate phone numbers found in rows: {rows_str}. Phone numbers in a campaign must be unique.",
                    invalid_rows=duplicate_rows,
                ),
            )

        return ValidationResult(is_valid=True, headers=normalized_headers, rows=rows)

    @staticmethod
    def validate_template_columns(
        headers: List[str],
        rows: List[List[str]],
        required_columns: Set[str],
    ) -> ValidationResult:
        """Validate that template variable columns exist and are non-empty in all rows."""
        normalized_headers = CampaignSourceSyncService.normalize_headers(headers)

        # Check for missing columns
        missing = required_columns - set(normalized_headers)
        if missing:
            missing_str = ", ".join(f"'{c}'" for c in sorted(missing))
            return ValidationResult(
                is_valid=False,
                error=ValidationError(
                    message=f"Workflow uses template variables that are missing from the source data: {missing_str}. "
                    "Add the missing columns or remove them from the workflow."
                ),
            )

        # Check for empty values in required columns
        col_indices = {col: normalized_headers.index(col) for col in required_columns}

        for col, idx in col_indices.items():
            empty_rows = []
            for row_idx, row in enumerate(rows, start=2):
                if len(row) <= idx or not row[idx].strip():
                    empty_rows.append(row_idx)

            if empty_rows:
                if len(empty_rows) > 5:
                    rows_str = f"{', '.join(map(str, empty_rows[:5]))} and {len(empty_rows) - 5} more"
                else:
                    rows_str = ", ".join(map(str, empty_rows))

                return ValidationResult(
                    is_valid=False,
                    error=ValidationError(
                        message=f"Template variable '{col}' is empty in rows: {rows_str}. "
                        "All template variables used in the workflow must have values in every row.",
                        invalid_rows=empty_rows,
                    ),
                )

        return ValidationResult(is_valid=True)

    @abstractmethod
    async def validate_source(
        self, source_id: str, organization_id: Optional[int] = None
    ) -> ValidationResult:
        """Validate source data before campaign creation."""
        pass

    @abstractmethod
    async def sync_source_data(self, campaign_id: int) -> int:
        """
        Fetches data from source and creates queued_runs
        Each record gets a unique source_uuid based on source type
        Returns: number of records synced
        """
        pass

    async def get_source_credentials(
        self, organization_id: int, source_type: str
    ) -> Dict[str, Any]:
        """Gets source credentials when a sync service requires them."""
        logger.info(
            f"Getting credentials for org {organization_id}, source {source_type}"
        )
        return {}
