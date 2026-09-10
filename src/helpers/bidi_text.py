import re
import unicodedata

# Unicode Bidirectional Isolate marks (Unicode 6.3+, the W3C-recommended
# mechanism for an inline foreign-script span inside RTL text — see
# UAX #9 and https://www.w3.org/International/questions/qa-bidi-unicode-controls).
# Deliberately NOT the older embedding controls (U+202A LRE / U+202C PDF):
# embeddings can "leak" direction into surrounding text in ways isolates are
# specifically designed to prevent, which is exactly the failure mode this
# function exists to close (a real, confirmed patient-facing WhatsApp
# readability bug — mixed-direction embedded terms like "CBCT (3D)" or
# "72 ساعة" rendering with mirrored parentheses / scrambled internal order
# when embedded in Arabic-dominant RTL prose).
# Written as explicit \u escapes, not literal invisible glyphs -- these
# are zero-width control characters with no visible representation in a
# normal editor; an escape sequence is self-evidently correct on inspection,
# where a literal invisible character is not.
_LRI = "\u2066"  # LEFT-TO-RIGHT ISOLATE
_PDI = "\u2069"  # POP DIRECTIONAL ISOLATE
# 2026-09-10 addendum \u2014 RIGHT-TO-LEFT MARK, the standard UAX #9 anchor for
# a NEUTRAL character (sentence punctuation, not a strong-direction letter
# of either script) that must resolve as part of the surrounding RTL run
# rather than floating free \u2014 see anchor_floating_punctuation's own
# docstring for the real incident this closes.
_RLM = "\u200f"  # RIGHT-TO-LEFT MARK


# 2026-09-10 \u2014 dynamic, content-agnostic script/direction classification.
# Every prior version of this module's script-boundary logic hand-typed a
# literal Arabic Unicode block range; this queries Python's own Unicode
# Character Database (stdlib `unicodedata`, the same database the real
# bidi algorithm itself is built from) per character instead, so it needs
# no maintained range table at all and automatically covers every
# character Unicode itself classifies as Arabic-letter/RTL or Latin-
# letter-or-digit/LTR \u2014 genuinely dynamic, not a longer hardcoded list.
def _is_rtl_char(ch: str) -> bool:
    """True for a character with a strong right-to-left Bidi_Class --
    "AL" (Arabic Letter) or "R" (Right-to-Left, e.g. Hebrew) -- per the
    real Unicode Bidi_Class property, not a hand-picked code-point range.
    Also true for the RLM mark itself (its own Bidi_Class is "R"), which
    is deliberate: a run this module just anchored with RLM must still
    read as "real RTL content here" to any later pass in this same
    pipeline (see ensure_script_transition_spacing's own docstring).
    False for "" (a string boundary, not a character) — callers pass "" for
    "there is no character here" (start/end of string) rather than special-
    casing that themselves."""
    return bool(ch) and unicodedata.bidirectional(ch) in ("AL", "R")


def _is_ltr_char(ch: str) -> bool:
    """True for a character with a strong left-to-right Bidi_Class --
    "L" (Latin/other LTR letters) or "EN" (European Number, i.e. Western
    ASCII digits) -- per the real Unicode Bidi_Class property. Arabic-
    Indic digits (\u0660-\u0669) are correctly excluded: Unicode classifies them
    "AN" (Arabic Number), a weak-RTL-associated class with none of this
    module's embedded-run problems, same reasoning _LATIN_RUN_RE's own
    docstring already gives for leaving them untouched. False for "" (a
    string boundary, not a character), same reasoning as _is_rtl_char."""
    return bool(ch) and unicodedata.bidirectional(ch) in ("L", "EN")


def _is_anchorable_punct_char(ch: str) -> bool:
    """True for a NEUTRAL punctuation character -- a period, comma,
    colon, semicolon, question/exclamation mark, in any script -- that is
    a real candidate for the "floating punctuation" fix
    (anchor_floating_punctuation). Detected dynamically via Unicode
    General Category ("P*" -- Po/Pd/Pc/Pf/Pi, but NOT Ps/Pe) or
    Bidi_Class ("ON"/"CS"/"ES"/"ET"), never a hand-typed list of specific
    punctuation characters -- covers "." "\u060c" "\u061b" "\u061f" "!" ":" "," and any
    other neutral separator Unicode itself classifies the same way,
    without this module needing to know about each one individually.

    Deliberately EXCLUDES open/close brackets (Unicode category "Ps"/
    "Pe" -- "(" ")" "[" "]" "{" "}" and their full-width/typographic
    equivalents): those already have their own dedicated, more surgical
    spacing/nesting rules in normalize_bilingual_punctuation, which runs
    BEFORE this function in sanitize_reply_text's own pipeline -- by the
    time this function sees the text, a glued paren has already been
    given its correct boundary space, so it's no longer "floating" and
    doesn't need (and shouldn't get) RLM-anchoring on top of that."""
    if ch.isspace():
        return False
    category = unicodedata.category(ch)
    if category in ("Ps", "Pe"):
        return False
    return category.startswith("P") or unicodedata.bidirectional(ch) in ("ON", "CS", "ES", "ET")

# Matches a maximal run of Latin letters/digits, with a small set of
# punctuation allowed ONLY in the interior or as a trailing character (never
# as the sole content) -- e.g. "CBCT (3D) Single Arch", "72", "25%", "3D".
# Deliberately ASCII-only: Arabic-Indic digits (٠-٩) are correctly left
# untouched -- Unicode already classifies them as "Arabic Number" (AN), a
# weak-RTL-associated class that doesn't have this problem; only Western
# digits/Latin letters (classified "EN"/"L", weak-LTR-associated) are the
# real source of the embedded-run corruption this closes.
#
# 2026-09-09 fix -- real, confirmed bug found via direct testing against a
# real catalog string ("... (Regular EEG - Three hour)"): the leading
# character class was [A-Za-z0-9] only, never "(" -- so a run starting
# with an opening paren had its "(" left OUTSIDE the isolate (in the
# surrounding Arabic RTL context) while the matching ")" landed INSIDE
# (the trailing class already included ")"). That asymmetry — one paren
# protected, the other not — is the exact, confirmed mechanism behind the
# "mismatched/double parentheses" symptom reported against real patient-
# facing replies. Adding "(" to the leading class closes it directly.
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9(](?:[A-Za-z0-9 \-/().&%:+]*[A-Za-z0-9%)])?")


def isolate_latin_runs(text: str) -> str:
    """Wraps every embedded Latin/technical-term run in `text` with a
    Unicode bidi isolate (LRI...PDI), so it renders as a self-contained
    left-to-right unit regardless of the surrounding Arabic (RTL) prose's
    own bidi resolution — real, confirmed incident this fixes: patient-
    facing WhatsApp replies mixing Arabic and inline English/technical
    terms (exam names, durations, abbreviations) were rendering jumbled —
    parentheses mirrored, characters within a term reordered — a real
    Unicode Bidirectional Algorithm (UAX #9) consequence of embedding
    weak-direction Latin/digit runs inside a resolved-RTL paragraph, not a
    data-level corruption.

    Deliberately does NOT reorder anything, and does NOT try to force the
    overall sentence/list to read in "typed" left-to-right order — a list
    embedded in RTL prose (e.g. a clarification menu) is CORRECTLY
    expected by a native Arabic reader to visually flow right-to-left
    overall; only the INTERNAL character order of each individual
    embedded term is the real, narrow problem being closed here. Every
    character of the real content is preserved exactly — the only
    addition is a pair of invisible (zero-width, never rendered as a
    glyph) control characters around each matched run.

    Idempotent: any pre-existing LRI/PDI marks are stripped before
    re-wrapping, so calling this twice on the same text (or on text this
    function already processed) yields byte-identical output, not a
    doubly-nested wrap. Caught by the function's own test: a first
    implementation matched runs by character class alone, which correctly
    never matched the LRI/PDI marks themselves, but still re-wrapped the
    *same* Latin run a second time on repeated application — harmless to
    rendering (nested same-direction isolates don't visually misbehave),
    but not actually idempotent as originally claimed. Stripping first
    closes that gap directly rather than leaving the claim unverified."""
    text = text.replace(_LRI, "").replace(_PDI, "")
    return _LATIN_RUN_RE.sub(lambda m: _LRI + m.group(0) + _PDI, text)


# 2026-09-09 addendum — real, reported symptoms: malformed/nested
# parentheses ("((Regular EEG - Three hour)") and stray edge hyphens
# ("(- Regular EEG One hour)") around embedded English terms. Each rule
# below targets a specific, narrow, real pattern — never a blanket
# character strip — so it can only ever remove genuinely redundant
# punctuation a well-formed string would never contain in the first
# place (a real "(Regular EEG - Three hour)" has no space directly after
# "(" or before ")", and no hyphen directly adjacent to either paren with
# no real word between them — these patterns only fire on the malformed
# shape, never on correct text).
_REPEATED_OPEN_PAREN_RE = re.compile(r"\(\s*\(+")
_REPEATED_CLOSE_PAREN_RE = re.compile(r"\)+\s*\)")
_STRAY_HYPHEN_AFTER_OPEN_PAREN_RE = re.compile(r"\(\s*-\s+")
_STRAY_HYPHEN_BEFORE_CLOSE_PAREN_RE = re.compile(r"\s+-\s*\)")
_SPACE_AFTER_OPEN_PAREN_RE = re.compile(r"\(\s+")
_SPACE_BEFORE_CLOSE_PAREN_RE = re.compile(r"\s+\)")
_STRAY_EDGE_HYPHEN_RE = re.compile(r"^\s*-\s+|\s+-\s*$")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")

# 2026-09-09, second readability round — real, reported live-UI defects
# beyond the parenthesis/hyphen shapes above:
#
# 1. A leading colon ("`:متاح لدينا`") — a colon can never legitimately
#    open a sentence/line (it always labels something that must come
#    BEFORE it), so one appearing as the very first character of the
#    whole reply or of any line is always a formatting defect, never real
#    content — safe to strip unconditionally, no false-positive case
#    exists for a well-formed string.
_LEADING_COLON_RE = re.compile(r"^:\s*", re.MULTILINE)

# 2. Orphaned/nested opening parens with real words between them and only
#    ONE real closing paren — e.g. "(Bone Scan(Mdp)" (should read
#    "Bone Scan (Mdp)"). Deliberately requires TWO literal "(" before the
#    first ")" (`[^()]*` excludes both paren characters from each captured
#    span, so this can only ever match when the first "(" is genuinely
#    unclosed before a second one opens) — a normal, well-formed single
#    "(...)" group has no interior "(" at all and never matches. Fixes the
#    real defect by dropping the earlier, orphaned "(" and keeping the
#    inner text plus the properly-closed second group; the missing-space
#    rule below then adds the boundary space the dropped "(" used to
#    (incorrectly) provide. Disclosed, narrow trade-off: a hypothetical
#    STRING with genuinely intentional nested parens ("(A (B) C)") would
#    also match and get its outer "(" stripped — real catalog data
#    inspected across this project's entire golden-suite/DB audit never
#    once used intentional nesting (only flat "Arabic (English)"
#    translations), so this is treated as an acceptable, disclosed trade
#    rather than a real regression risk.
_ORPHAN_OPEN_PAREN_RE = re.compile(r"\(([^()]*)\(([^()]*)\)")

# 3/4. Missing space at a paren boundary — the mirror image of rules 5/6
#      below: real reported defects glued a word directly onto "(" with
#      NO space at all ("scintigraphy(مسح", "مراريه(") or glued the word
#      right after "" with no space ("...hour)بالفرع"). `(?<=[^\s(])`
#      requires a real, non-space, non-"(" character immediately before
#      the paren, so this never fires on already-correct text (where a
#      space or another opening paren already precedes it) or at the very
#      start of the string.
_MISSING_SPACE_BEFORE_OPEN_PAREN_RE = re.compile(r"(?<=[^\s(])\(")
_MISSING_SPACE_AFTER_CLOSE_PAREN_RE = re.compile(r"\)(?=[^\s).,،؛:؟!\-])")

# 5. Two scripts glued directly together with zero separating space at all
#    (not a paren-boundary case — plain "ArabicMRCP"-shape gluing) used to
#    be handled right here via a hand-typed Arabic Unicode-range regex.
#    2026-09-10: replaced with the fully dynamic, stdlib-Unicode-property-
#    driven ensure_script_transition_spacing below (see that function's
#    own docstring) — no range table maintained in this module at all
#    anymore, so this step now lives in its own function, run separately
#    by sanitize_reply_text rather than inline here.

# 6. "Dense, cluttered text blocks lacking clean visual spacing or natural
#    sentence breaks" — a genuinely single-block reply (no newlines of its
#    own at all — a multi-line clarification list already has its own
#    deliberate per-option line structure and must never be touched by
#    this) gets a paragraph break inserted after each real sentence
#    terminator. Requires real trailing content after the terminator (a
#    reply's own final full stop never matches, since there's nothing
#    after it), so this only ever splits an actual multi-sentence block,
#    never adds a break to an already-short single-sentence reply.
_SENTENCE_BREAK_RE = re.compile(r"([.؟!])[ \t]+(?=\S)")


def normalize_bilingual_punctuation(text: str) -> str:
    """Cleans up malformed punctuation/spacing around an embedded English
    or technical term inside Arabic text — real, confirmed incidents this
    fixes: double/nested and orphaned parentheses ("((Regular EEG - Three
    hour)", "(Bone Scan(Mdp)"), a stray leading hyphen inside a
    parenthetical ("(- Regular EEG One hour)"), a stray leading colon
    ("`:متاح لدينا`"), and a word glued directly onto a paren with no
    separating space at all ("scintigraphy(مسح", "مراريه(") — all observed
    in real patient-facing replies, across both standard direct answers
    and the clarification menu (both pass through this SAME function —
    see _build_clarification_reply's own per-suffix call — so a fix here
    benefits both output shapes uniformly, never one at the expense of
    the other). Paren-UNRELATED script-boundary gluing ("ArabicMRCP") and
    floating/displaced sentence punctuation are separate, dynamically-
    driven concerns handled by ensure_script_transition_spacing and
    anchor_floating_punctuation respectively — see sanitize_reply_text's
    own docstring for why all of these run as a single ordered pipeline
    rather than one do-everything function. Never touches real content —
    every rule here only
    collapses a redundant punctuation mark, or ADDS a boundary space a
    well-formed string would already have, never rewrites/reorders a real
    word or fact, so a correctly-formed reply is left byte-for-byte
    unchanged (see this module's own regex comments for why each pattern
    is specific, not a blanket strip).

    Content-preserving and safe to call on any text, not just a known-
    malformed one — a clean string passes through with no changes,
    confirmed by this function's own idempotency (a second call on
    already-normalized output is a no-op, since none of the malformed
    patterns can exist in already-clean text — including the two ADDING
    rules: once a boundary space exists, the "missing space" lookbehind/
    lookahead conditions can no longer match).

    Ordered deliberately: the orphaned-paren fix runs before the
    repeated-paren collapse (so a triple-nested edge case reduces in the
    right direction, not left half-fixed), and every space-adding rule
    runs after every space-removing one, so an added space is never itself
    immediately stripped back out by a rule that ran later."""
    text = _LEADING_COLON_RE.sub("", text)
    text = _ORPHAN_OPEN_PAREN_RE.sub(r"\1(\2)", text)
    text = _REPEATED_OPEN_PAREN_RE.sub("(", text)
    text = _REPEATED_CLOSE_PAREN_RE.sub(")", text)
    text = _STRAY_HYPHEN_AFTER_OPEN_PAREN_RE.sub("(", text)
    text = _STRAY_HYPHEN_BEFORE_CLOSE_PAREN_RE.sub(")", text)
    text = _SPACE_AFTER_OPEN_PAREN_RE.sub("(", text)
    text = _SPACE_BEFORE_CLOSE_PAREN_RE.sub(")", text)
    text = _MISSING_SPACE_BEFORE_OPEN_PAREN_RE.sub(" (", text)
    text = _MISSING_SPACE_AFTER_CLOSE_PAREN_RE.sub(") ", text)
    text = _STRAY_EDGE_HYPHEN_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text.strip()


def anchor_floating_punctuation(text: str) -> str:
    """"Punctuation displacement and floating periods" (real, reported
    live-UI defect — a trailing/leading period, comma, colon, etc.
    visually detaching from the Arabic sentence it belongs to and
    appearing to "float" at a line-wrap boundary, e.g. a period from the
    end of one sentence rendering at the START of the next visual line:
    ".فمش هقدر"). This is a genuine Unicode Bidirectional Algorithm
    (UAX #9) consequence, not literal text corruption — a NEUTRAL
    character (Bidi_Class "ON"/"CS"/"ES"/"ET": most punctuation) has no
    strong direction of its own, so the algorithm resolves it from
    whichever strong-direction run happens to be nearest at RENDER time;
    at a line-wrap boundary that can put visual distance between the
    punctuation and the Arabic word it's actually glued to in the
    underlying text, letting a distant LTR run or the paragraph's own
    base direction win instead.

    Fixed the same way isolate_latin_runs fixes the analogous problem for
    embedded Latin runs, but with the RIGHT-TO-LEFT MARK (RLM, U+200F)
    instead of an isolate pair: RLM is itself a strong-RTL, zero-width,
    never-rendered-as-a-glyph character — per UAX #9 rules N1/N2, a
    neutral character flanked by RLM on both sides resolves as RTL
    unconditionally, regardless of how the surrounding paragraph
    ultimately wraps. Fires ONLY on a punctuation run genuinely GLUED
    (zero space) to a real Arabic/RTL character on at least one side —
    detected dynamically via _is_anchorable_punct_char/_is_rtl_char
    (Unicode category/Bidi_Class lookups, no hardcoded punctuation or
    script list) — so normal, already-correctly-spaced punctuation (a
    period after an English word, a comma with real content on neither
    side that's Arabic) is never touched.

    Deliberately excludes brackets (see _is_anchorable_punct_char's own
    docstring) — those are normalize_bilingual_punctuation's job, which
    runs BEFORE this function in sanitize_reply_text's pipeline, so a
    glued paren has already been given a real boundary space by the time
    this function sees the text and is no longer a "floating" candidate.

    Idempotent: strips any pre-existing RLI/PDI/RLM marks before
    re-scanning, matching isolate_latin_runs's own convention — so this
    is safe to call on text that already went through this pipeline once."""
    text = text.replace(_LRI, "").replace(_PDI, "").replace(_RLM, "")
    chars = list(text)
    total = len(chars)
    output = []
    index = 0
    while index < total:
        char = chars[index]
        if _is_anchorable_punct_char(char):
            run_end = index
            while run_end < total and _is_anchorable_punct_char(chars[run_end]):
                run_end += 1
            run = "".join(chars[index:run_end])
            prev_char = chars[index - 1] if index > 0 else ""
            next_char = chars[run_end] if run_end < total else ""
            if _is_rtl_char(prev_char) or _is_rtl_char(next_char):
                output.append(_RLM + run + _RLM)
            else:
                output.append(run)
            index = run_end
        else:
            output.append(char)
            index += 1
    return "".join(output)


def ensure_script_transition_spacing(text: str) -> str:
    """"Awkward or missing spacing around script transitions between LTR
    (English/numbers) and RTL (Arabic)" — inserts a single space wherever
    a strong-RTL character (Arabic letter) sits directly glued (zero
    space) against a strong-LTR character (Latin letter or Western
    digit), in either direction — e.g. "MRCPفي" -> "MRCP في",
    "بـHeidelberg" -> "بـ Heidelberg". Classification is a per-character
    Unicode Bidi_Class lookup (_is_rtl_char/_is_ltr_char, stdlib
    `unicodedata` — the same database the real bidi algorithm is built
    from), never a maintained list of specific words/acronyms or a
    hand-typed script range — genuinely dynamic: it works identically for
    "CT", "Heidelberg spectralis OCT-RNFL", a future exam name never seen
    before, or any other Latin term, with zero code change required.

    Deliberately narrower than a general "insert space between any two
    different categories" rule: only fires on two actual LETTER/DIGIT
    classes meeting with nothing between them. Punctuation (parens,
    periods, RLM/isolate marks) is excluded on purpose — paren boundaries
    already have their own dedicated spacing convention in
    normalize_bilingual_punctuation (space before "(", none after; none
    before ")", space after only if more text follows), and this
    function running after that one, and after anchor_floating_punctuation,
    would otherwise blindly override those deliberate choices. Safe to run
    after anchor_floating_punctuation despite RLM's own Bidi_Class being
    "R" (see _is_rtl_char's own docstring) — an RLM directly followed by a
    glued Latin letter (e.g. a period anchored right before "CT" with no
    space) correctly still gets a real space inserted there, which is the
    same fix a human editor would make by hand, not a side effect to
    guard against."""
    chars = list(text)
    output = []
    previous_char = ""
    for char in chars:
        if (
            previous_char
            and not previous_char.isspace()
            and not char.isspace()
            and (
                (_is_rtl_char(previous_char) and _is_ltr_char(char))
                or (_is_ltr_char(previous_char) and _is_rtl_char(char))
            )
        ):
            output.append(" ")
        output.append(char)
        previous_char = char
    return "".join(output)


def add_natural_paragraph_spacing(text: str) -> str:
    """"Dense, cluttered text blocks lacking clean visual spacing or
    natural sentence breaks" (real, reported standard-reply defect) —
    inserts a paragraph break (blank line) after a real sentence
    terminator that has more sentence content following it.

    Guarded to a single-line input only (`"\\n" not in text`): a
    clarification reply already has its own deliberate, fully-controlled
    per-option line structure (_build_clarification_reply) and must never
    have this layered on top of it — this function is a no-op for that
    shape, by construction, not by coincidence. Only ever ADDS a line
    break at a real sentence boundary; never removes or reorders content,
    so it carries zero factual-accuracy risk."""
    if "\n" in text:
        return text
    return _SENTENCE_BREAK_RE.sub(lambda m: f"{m.group(1)}\n\n", text)


def sanitize_reply_text(text: str) -> str:
    """Single, uniform post-generation sanitization entry point for every
    patient-facing WhatsApp reply — a standard direct answer and a
    clarification menu both pass through the exact same five-step
    pipeline, so a fix to any one step benefits both output shapes
    identically rather than needing parallel sanitizers to stay in sync
    by hand:

    1. normalize_bilingual_punctuation — literal malformed-punctuation
       shapes (double/orphaned parens, stray hyphens, leading colon,
       paren-boundary spacing).
    2. anchor_floating_punctuation — RLM-anchors sentence punctuation
       genuinely glued to Arabic, so it can't visually float to a
       line-wrap boundary.
    3. ensure_script_transition_spacing — dynamic Unicode-Bidi_Class-
       driven single-space insertion at any remaining glued Arabic/Latin
       letter-or-digit boundary.
    4. add_natural_paragraph_spacing — paragraph breaks for a dense,
       multi-sentence single-block reply (no-op for the clarification
       menu's own multi-line shape).
    5. isolate_latin_runs — wraps embedded Latin/technical-term runs in
       bidi isolates, always LAST.

    Order matters throughout: literal punctuation defects (1) must be
    fixed before floating-punctuation anchoring (2) so a glued paren
    isn't mistaken for floating sentence punctuation (see
    _is_anchorable_punct_char's own docstring for why brackets are
    excluded from step 2 entirely); steps 2 and 3 must both run on PLAIN
    text — before any invisible bidi mark exists — so their character-
    adjacency checks see real neighbors, not a mark; paragraph spacing (4)
    must run after punctuation is already correct, so it looks for real
    sentence terminators, not malformed ones; and isolation (5) must run
    strictly last, since it wraps Latin runs in invisible bidi marks, and
    every step above operates on plain, visible characters only."""
    text = normalize_bilingual_punctuation(text)
    text = anchor_floating_punctuation(text)
    text = ensure_script_transition_spacing(text)
    text = add_natural_paragraph_spacing(text)
    return isolate_latin_runs(text)
