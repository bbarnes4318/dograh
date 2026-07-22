from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional, Set

from loguru import logger


@dataclass
class ValidationError:
    """Represents a validation error with details."""

    message: str
    invalid_rows: Optional[List[int]] = None


@dataclass
class ValidationResult:
    """Result of source validation."""

    is_valid: bool
    error: Optional[ ValidationError] = None
    headers: Optional[List[str]] = field(default=None, repr=False)
    rows: Optional[ List[List[str]]] = field(default=None, repr=False)


class CampaignSourceSyncService(ABC):
    """Base class for campaign data source synchronization"""

    @staticmethod
    def normalize_headers(headers: List[str]) -> List[str]:
        """Normalize headers by stripping whitespace and lowercasing, mapping common aliases to phone_number."""
        normalized = []
        for h in headers:
            clean = h.strip().lower().replace(" ", "_").replace("-", "_")
            if clean in ["phone", "phone_number", "phonenumber", "mobile", "cell", "telephone", "phone_no", "contact_number", "destination"]:
                normalized.append("phone_number")
            else:
                normalized.append(clean)
        return normalized

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
        if "phone_number" not in normalized_headers:
            return ValidationResult(
                is_valid=False,
                error=ValidationError(
                    message="Source must contain a 'phone_number' column"
                ),
            )

        phone_number_idx = normalized_headers.index("phone_number")

        # Validate phone numbers in all data rows
        invalid_rows = []
        for row_idx, row in enumerate(
            rows, start=2
        ):  # Start at 2 (1-indexed, skip header)
            if len(row) <= phone_number_idx:
                continue  # Skip rows that don't have enough columns

            phone_number = row[phone_number_idx].strip()
            clean_digits = re.sub(r"[v\d+]", "", phone_number)
            if clean_digits and not clean_digits.startswith("+"):
                if not (len(clean_digits) in (10, 11) and clean_digits.lstrip("+").isdigit()):
                    invalid_rows.append(row_idx)

        if invalid_rows:
            # Limit the number of rows shown in error message
            if len(invalid_rows) > 5:
                rows_str = ", ".join(map(str, invalid_rows[:5])) + f" and {len(invalid_rows) - 5} more"
            else:
                rows_str = ", ".join(map(str, invalid_rows))
            return ValidationResult(
                is_valid=False,
                error=ValidationError(
                    message=f"Phone numbers must start with + (found invalid numbers at rows: {rows_str})",
                    invalid_rows=invalid_rows,
                ),
            )

        return ValidationResult(is_valid=True, headers=headers, rows=rows)

    @abstractmethod
    async def validate_source(
        self, source_id: str, organization_id: Optional[int] = None
    ) -> ValidationResult:
        """Validate that a data source exists, is accessible, and has valid schema."""
        pass

    @abstractmethod
    async def sync_source_data(self, campaign_id: int) -> int:
        """Synchronize data from source and return number of records created."""
        pass
