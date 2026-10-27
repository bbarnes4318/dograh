from pydantic import BaseModel


class OrganizationPreferences(BaseModel):
    test_phone_number: str | None = None
    timezone: str | None = None
    external_pbx_integrations_enabled: bool = False
    # Answer only inbound callers Hopwhistle has screened (TCPA litigator
    # list) and handed over; refuse every other inbound call. Off by default.
    # See services/telephony/inbound_admission.py.
    inbound_screening_required: bool = False
