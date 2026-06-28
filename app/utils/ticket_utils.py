from __future__ import annotations

import re


_CURRENT_TICKET_PATTERNS = [
    re.compile(r"(?im)^\s*-?\s*Ticket Jira courant\s*:\s*([A-Z][A-Z0-9]+-\d+)\b"),
    re.compile(r"(?im)^\s*-?\s*Current Jira ticket\s*:\s*([A-Z][A-Z0-9]+-\d+)\b"),
    re.compile(r"(?im)^\s*-?\s*Ticket courant\s*:\s*([A-Z][A-Z0-9]+-\d+)\b"),
    re.compile(r"(?im)^\s*-?\s*Ticket\s*:\s*([A-Z][A-Z0-9]+-\d+)\b"),
]

_TICKET_FINDER = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def extract_current_ticket_key(text: str) -> str | None:
    """v9.13.1: extraction du ticket courant avec strategie de priorite.

    Bug observe TICKET-NNNNN : le rapport contient plusieurs tickets (le courant
    + des incidents similaires comme TICKET-NNNNN, TICKET-30061). L'ancien code
    retournait None si plus d'un ticket, ce qui faisait perdre le nommage de
    branche `hotfix/RUN-XXXXX` au profit d'un nommage generique base sur le
    titre du LLM ("hotfix/correction-de-nullp...").

    Nouvelle strategie en cascade :
      1. Chercher dans les patterns explicites "Ticket courant : RUN-XXXXX"
      2. Si plusieurs tickets dans le texte, prendre celui qui apparait EN PREMIER
         dans les 500 premiers caracteres (le titre du rapport)
      3. Si toujours rien, prendre le 1er ticket trouve dans tout le texte
    """
    for pattern in _CURRENT_TICKET_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).upper()

    # v9.13.1 : chercher d'abord dans le titre (500 premiers chars)
    head = text[:500].upper()
    head_match = _TICKET_FINDER.search(head)
    if head_match:
        return head_match.group(1).upper()

    # Sinon, chercher dans tout le texte
    unique_keys: list[str] = []
    for key in _TICKET_FINDER.findall(text.upper()):
        if key not in unique_keys:
            unique_keys.append(key)
    if len(unique_keys) == 1:
        return unique_keys[0]
    # v9.13.1 : si plusieurs tickets, prendre le PREMIER dans l'ordre du texte
    # (le ticket courant est typiquement mentionne avant les incidents similaires)
    if len(unique_keys) >= 2:
        return unique_keys[0]
    return None
