from __future__ import annotations

import hashlib

from sqlalchemy import func, select

from src.agents.base import BaseAgent
from src.database.models import Company, Contact, MarketProfile, Prospect, Research
from src.models.enums import ClaimLabel
from src.models.schemas import DossierOut, MarketProfileOut, SourceDoc
from src.services.evidence import verify_claims
from src.services.prompts import untrusted


class ResearcherAgent(BaseAgent):
    name = "researcher"
    prompt_name = "researcher"

    def run(self, prospect: Prospect) -> Research:
        company = self.s.get(Company, prospect.company_id)
        profile_row = self.s.get(MarketProfile, prospect.market_profile_id) if prospect.market_profile_id else None
        profile = profile_row.profile if profile_row else {}
        with self.trace(prospect_id=prospect.id, input_summary=f"research {company.name} site={company.website}") as tr:
            docs: list[SourceDoc] = self.svc.fetcher.fetch_company_pages(company.website or "")
            tr.tool_call("fetch_company_pages", {"website": company.website}, f"{len(docs)} documents")
            contacts = list(self.s.scalars(select(Contact).where(Contact.company_id == company.id)))
            company_ctx = {"name": company.name, "website": company.website, "location": company.location,
                           "industry_label": company.industry, "employee_estimate_from_source": company.employee_estimate,
                           "known_decision_maker": next((c.name for c in contacts if c.is_decision_maker and c.name), None)}
            if docs:
                src = "\n\n".join(untrusted(f"URL: {d.url}\n{d.text}", "source_document") for d in docs)
                out = self.ask(tr, DossierOut, {"company": company_ctx, "market_profile": profile, "source_documents": src},
                               "Build the dossier.", max_tokens=3500)
            else:
                out = DossierOut(summary="No source documents could be retrieved.", unknowns=["everything: no accessible sources"],
                                 decision_summary="No sources; skipped LLM to avoid fabrication.")
            claims = verify_claims(out.claims, docs)
            downgraded = sum(1 for a, b in zip(out.claims, claims) if a.label != b.label)
            version = (self.s.scalar(select(func.max(Research.version)).where(Research.prospect_id == prospect.id)) or 0) + 1
            row = Research(
                prospect_id=prospect.id, version=version,
                dossier={"summary": out.summary, "claims": [c.model_dump(mode="json") for c in claims], "unknowns": out.unknowns},
                sources=[{"url": d.url, "chars": len(d.text), "sha256": hashlib.sha256(d.text.encode()).hexdigest()} for d in docs],
            )
            self.s.add(row)
            self.s.flush()
            facts = sum(1 for c in claims if c.label == ClaimLabel.FACT)
            tr.set_output({"research_id": row.id, "claims": len(claims), "facts": facts, "downgraded": downgraded},
                          out.decision_summary or f"{facts} verified facts from {len(docs)} sources")
            return row
