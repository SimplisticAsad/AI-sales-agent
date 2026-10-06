from __future__ import annotations

import re
from urllib.parse import urlparse

from sqlalchemy import func, select

from src.agents.base import BaseAgent
from src.database.models import Company, Contact, Deal, MarketProfile, Prospect
from src.integrations.prospects.sources import ProspectSource
from src.models.schemas import HunterOut, MarketProfileOut, RawCandidate
from src.services.prompts import untrusted
from src.services.qualification import score_prospect

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def norm_domain(website: str | None) -> str | None:
    if not website:
        return None
    u = urlparse(website if "://" in website else f"https://{website}")
    host = (u.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


class ProspectHunterAgent(BaseAgent):
    """Sources return raw records; this agent normalises them. Contact data and sizes are never LLM-generated."""

    name = "prospect_hunter"
    prompt_name = "prospect_hunter"

    def _existing(self, raw: RawCandidate) -> Prospect | None:
        dom = norm_domain(raw.website)
        q = select(Company)
        for c in self.s.scalars(q):
            if (dom and norm_domain(c.website) == dom) or (not dom and c.name.lower() == raw.company_name.lower()):
                return self.s.scalar(select(Prospect).where(Prospect.company_id == c.id))
        return None

    def ingest(self, raw: RawCandidate, profile_row: MarketProfile | None = None) -> Prospect:
        if (dup := self._existing(raw)):
            return dup
        profile = MarketProfileOut(**profile_row.profile) if profile_row else MarketProfileOut(
            industry="", employee_min=1, employee_max=100000)
        with self.trace(input_summary=f"candidate={raw.company_name} source={raw.source}") as tr:
            cand_text = f"Name: {raw.company_name}\nWebsite: {raw.website}\nLocation: {raw.location}\n{raw.text}"
            out = self.ask(
                tr, HunterOut,
                {"market_profile": profile.model_dump(), "candidate": untrusted(cand_text, "candidate")},
                "Extract fields.",
            ).extracted
            blob = (raw.text + " " + (raw.contact_name or "")).lower()
            dm = out.decision_maker if out.decision_maker and out.decision_maker.lower() in blob else raw.contact_name
            dm_role = raw.contact_role or (out.decision_maker_role if dm == out.decision_maker else None)
            email = raw.email if raw.email and _EMAIL.match(raw.email) else None
            industry = out.industry or (profile.industry or None)

            res = score_prospect(
                profile, industry=out.industry, location=raw.location, employees=raw.employee_estimate,
                website=raw.website, has_email=bool(email), has_phone=bool(raw.phone),
                has_contact_page=bool(raw.contact_page_url), decision_maker=dm,
            )
            company = Company(name=raw.company_name, website=raw.website, industry=industry, location=raw.location,
                              employee_estimate=raw.employee_estimate, source=raw.source)
            self.s.add(company)
            self.s.flush()
            if email or raw.phone or raw.contact_page_url or dm:
                self.s.add(Contact(
                    company_id=company.id, name=dm, role=dm_role, email=email, phone=raw.phone,
                    contact_page_url=raw.contact_page_url, source=raw.source or None, is_decision_maker=bool(dm),
                ))
            prospect = Prospect(
                company_id=company.id, market_profile_id=profile_row.id if profile_row else None, source=raw.source,
                qualification_score=res.score,
                qualification={"stage": "discovery", "breakdown": res.breakdown, "reasons": res.reasons,
                               "problem_hints": out.problem_hints},
            )
            self.s.add(prospect)
            self.s.flush()
            self.s.add(Deal(prospect_id=prospect.id, state="DISCOVERED"))
            self.s.flush()
            tr.run.prospect_id = prospect.id
            tr.set_output({"company": raw.company_name, "score": res.score, "reasons": res.reasons},
                          out.decision_summary or f"Discovered {raw.company_name} with ICP score {res.score}")
            return prospect

    def hunt(self, source: ProspectSource, profile_row: MarketProfile, limit: int = 25) -> list[Prospect]:
        profile = MarketProfileOut(**profile_row.profile)
        return [self.ingest(raw, profile_row) for raw in source.search(profile, limit)]
