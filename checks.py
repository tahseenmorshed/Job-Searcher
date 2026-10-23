"""Check an assessment's quotes against the listing and resume it came from.

Quotes are only there to show the model didn't invent the text, so they're matched loosely:
a fragment passes if most of its words appear close together in the source, in any order,
ignoring case and punctuation. Whether the text means what the requirement asks is the
model's job (the match rating), not this module's.

Like scoring.py, this runs whenever results are shown, so changing a rule re-checks
every saved assessment with no new AI calls.
"""

import re
from collections import Counter

# Share of a fragment's words that must appear near each other in the source.
MIN_WORDS_FOUND = 0.8
# How far apart the words may be: the search window is this many times the fragment's length.
WINDOW_FACTOR = 2

EVIDENCE_MATCHES = {"direct", "transferable", "side_project", "stretch"}
# Linking words a rewording adds or drops ("design of REST APIs" vs "REST API design"); not counted.
SMALL_WORDS = {"a", "an", "the", "of", "and", "or", "in", "on", "for", "to", "with", "at", "by", "from", "as"}


def words(text):
    """Lowercase words with punctuation removed and a plural 's' dropped, so 'APIs' matches 'API'."""
    out = []
    for w in re.findall(r"[a-z0-9+#]+", (text or "").lower()):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def found_in(fragment, source_words):
    """True if at least MIN_WORDS_FOUND of the fragment's words appear in some nearby stretch of the source."""
    frag = [w for w in words(fragment) if w not in SMALL_WORDS] or words(fragment)
    if not frag:
        return False
    need = Counter(frag)
    required = MIN_WORDS_FOUND * len(frag)
    window = max(len(frag) * WINDOW_FACTOR, len(frag) + 3)
    # Slide a window along the source, keeping a running count of the fragment's words inside it.
    have = Counter()
    hits = 0
    for i, w in enumerate(source_words):
        if w in need:
            have[w] += 1
            if have[w] <= need[w]:
                hits += 1
        if i >= window:
            old = source_words[i - window]
            if old in need:
                if have[old] <= need[old]:
                    hits -= 1
                have[old] -= 1
        if hits >= required:
            return True
    return False


def check_criterion(c, listing_words, resume_words):
    """Return a list of plain-English flags for one criterion (empty if it looks fine)."""
    flags = []
    if c.get("listing_quote") and not found_in(c["listing_quote"], listing_words):
        flags.append(f'Listing quote not found in the listing: "{c["listing_quote"]}"')
    quotes = c.get("resume_quotes") or []
    for q in quotes:
        if not found_in(q, resume_words):
            flags.append(f'Resume quote not found in the resume: "{q}"')
    if c["match"] in EVIDENCE_MATCHES and not quotes:
        flags.append(f"Rated {c['match']} with no resume quote behind it")
    if c["match"] == "none" and quotes:
        flags.append("Rated none, but resume quotes were given")
    return flags


def resume_from_system(system_text):
    """The resume as the model saw it: the text inside <resume> tags in the saved prompt."""
    m = re.search(r"<resume>\n?(.*?)\n?</resume>", system_text or "", re.S)
    return m.group(1) if m else ""


def check_assessment(a, listing_text, resume_text):
    """Add a 'flags' list to each criterion and a total 'flag_count' to the assessment."""
    listing_words = words(listing_text)
    resume_words = words(resume_text)
    total = 0
    for c in a["criteria"]:
        c["flags"] = check_criterion(c, listing_words, resume_words)
        total += len(c["flags"])
    a["flag_count"] = total
    return a
