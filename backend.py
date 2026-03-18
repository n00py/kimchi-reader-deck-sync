from __future__ import annotations

import base64
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib import error, parse, request
from urllib.parse import quote


KIMCHI_AUTH_API_BASE_URL = "https://api.kimchi-reader.app/v1"
KIMCHI_API_BASE_URL = "https://api.kimchi-reader.app"
KIMCHI_ORIGIN = "https://kimchi-reader.app"
TOKEN_REFRESH_WINDOW = timedelta(minutes=30)
KIMCHI_STATUS_SEEN = 1
KIMCHI_STATUS_KNOWN = 2
KIMCHI_STATUS_BULK_LIMIT = 500
KIMCHI_HISTORY_PAGE_LIMIT = 5000
STATE_FILE_NAME = "state.json"
INVALID_WORD_CACHE_FILE_NAME = "kimchi_invalid_words.json"
RUNS_DIR_NAME = "runs"
COLLECTION_FILE_NAMES = ("collection.anki21", "collection.anki2")

SOUND_TAG_RE = __import__("re").compile(r"\[sound:[^\]]+\]")
HTML_TAG_RE = __import__("re").compile(r"<[^>]+>")
BRACKETED_TEXT_RE = __import__("re").compile(r"\[[^\]]+\]")
KOREAN_TOKEN_RE = __import__("re").compile(r"[가-힣]+")


class KimchiAddonError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncSettings:
    mature_interval_days: int = 21
    max_korean_tokens: int = 2
    kimchi_lemmatize_batch_size: int = 500
    kimchi_status_batch_size: int = 500
    explicit_field_priority: Tuple[str, ...] = (
        "Korean",
        "Vocab",
        "Disambig-Korean",
    )
    generic_field_priority: Tuple[str, ...] = (
        "Front",
        "FrontText",
    )
    model_field_overrides: Dict[str, Tuple[str, ...]] = field(
        default_factory=lambda: {
            "Basic (and reversed card)": ("Front",),
            "Basic (and reversed card)+": ("Korean", "Front"),
            "Basic (and reversed card) (genanki)": ("Korean", "Front"),
            "Basic Quizlet Extended": ("FrontText",),
            "Korean Audio Model+": ("Korean",),
            "Talk To Me In Korean": ("Korean",),
        }
    )


@dataclass(frozen=True)
class SourceCard:
    card_id: int
    note_id: int
    deck_name: str
    model_name: str
    fields: Dict[str, str]
    interval: int
    reps: int


@dataclass(frozen=True)
class CandidateEntry:
    expression: str
    source_text: str
    normalized_source: str
    selected_field: str
    deck_name: str
    model_name: str
    card_id: int
    note_id: int
    interval: int
    reps: int
    token_count: int


@dataclass(frozen=True)
class SkippedEntry:
    reason: str
    message: str
    deck_name: str
    model_name: str
    card_id: int
    note_id: int
    fields_considered: Tuple[str, ...] = ()
    source_text: str = ""


@dataclass(frozen=True)
class PlannedExpression:
    expression: str
    status: str
    entries: Tuple[CandidateEntry, ...]


@dataclass(frozen=True)
class PlannedExpressionSkip:
    expression: str
    reason: str
    entries: Tuple[CandidateEntry, ...]


@dataclass(frozen=True)
class SyncPlan:
    selected_decks: Tuple[str, ...]
    candidates: Tuple[CandidateEntry, ...]
    skipped: Tuple[SkippedEntry, ...]
    planned: Tuple[PlannedExpression, ...]
    planning_skips: Tuple[PlannedExpressionSkip, ...] = ()

    @property
    def seen_expressions(self) -> Tuple[str, ...]:
        return tuple(
            item.expression for item in self.planned if item.status == "seen"
        )

    @property
    def known_expressions(self) -> Tuple[str, ...]:
        return tuple(
            item.expression for item in self.planned if item.status == "known"
        )


@dataclass(frozen=True)
class ImportStepResult:
    status: str
    submitted_count: int
    success: bool
    details: Optional[str] = None
    submitted_words: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewStepResult:
    status: str
    expression_count: int
    resolved_word_count: int
    already_marked_count: int
    cached_invalid_count: int
    pending_words: Tuple[str, ...] = ()
    details: Optional[str] = None


@dataclass(frozen=True)
class SyncPreview:
    selected_decks: Tuple[str, ...]
    plan: SyncPlan
    steps: Tuple[PreviewStepResult, ...]

    @property
    def pending_seen_words(self) -> Tuple[str, ...]:
        for step in self.steps:
            if step.status == "seen":
                return step.pending_words
        return ()

    @property
    def pending_known_words(self) -> Tuple[str, ...]:
        for step in self.steps:
            if step.status == "known":
                return step.pending_words
        return ()

    @property
    def total_pending_words(self) -> int:
        return len(self.pending_seen_words) + len(self.pending_known_words)

    @property
    def total_already_marked(self) -> int:
        return sum(step.already_marked_count for step in self.steps)

    @property
    def total_cached_invalid(self) -> int:
        return sum(step.cached_invalid_count for step in self.steps)


@dataclass(frozen=True)
class SyncRunResult:
    selected_decks: Tuple[str, ...]
    plan: SyncPlan
    steps: Tuple[ImportStepResult, ...]
    artifacts_dir: str

    @property
    def submitted_seen_words(self) -> Tuple[str, ...]:
        for step in self.steps:
            if step.status == "seen":
                return step.submitted_words
        return ()

    @property
    def submitted_known_words(self) -> Tuple[str, ...]:
        for step in self.steps:
            if step.status == "known":
                return step.submitted_words
        return ()


@dataclass(frozen=True)
class AuthTokens:
    access_token: Optional[str]
    refresh_token: Optional[str]


def strip_markup(value: str) -> str:
    value = html.unescape(value or "")
    value = SOUND_TAG_RE.sub(" ", value)
    value = HTML_TAG_RE.sub(" ", value)
    value = BRACKETED_TEXT_RE.sub(" ", value)
    value = value.replace("&nbsp;", " ")
    value = value.replace("\u00a0", " ")
    return " ".join(value.split()).strip()


def extract_korean_tokens(value: str) -> List[str]:
    return KOREAN_TOKEN_RE.findall(value or "")


def normalize_expression(value: str) -> str:
    return " ".join(extract_korean_tokens(strip_markup(value)))


def choose_reduced_word(original: str, reductions: object) -> Optional[str]:
    del original

    if not isinstance(reductions, list):
        return None

    words = []
    seen_words = set()
    summary_ids_by_word = {}

    for reduction in reductions:
        if not isinstance(reduction, dict):
            continue
        word = str(reduction.get("word") or "").strip()
        if not word:
            continue
        if word not in seen_words:
            seen_words.add(word)
            words.append(word)

        summary_ids = summary_ids_by_word.setdefault(word, set())
        summary = reduction.get("summary")
        if isinstance(summary, dict) and summary.get("id") is not None:
            summary_type = str(summary.get("type") or "")
            summary_ids.add("%s:%s" % (summary_type, summary["id"]))

    if len(words) != 1:
        return None

    word = words[0]
    if len(summary_ids_by_word.get(word, set())) > 1:
        return None
    return word


def extract_words_from_lemmatize_entry(entry: object) -> Tuple[str, ...]:
    items = []
    if isinstance(entry, dict):
        items = [entry]
    elif isinstance(entry, list):
        items = [item for item in entry if isinstance(item, dict)]

    words = []
    seen = set()
    for item in items:
        original = str(item.get("original") or "").strip()
        selected = choose_reduced_word(original, item.get("word_reduction"))
        if selected and selected not in seen:
            seen.add(selected)
            words.append(selected)
    return tuple(words)


def extract_words_from_lemmatize_response(payload: object) -> Tuple[str, ...]:
    if not isinstance(payload, list):
        return ()

    words = []
    seen = set()
    for entry in payload:
        for word in extract_words_from_lemmatize_entry(entry):
            if word in seen:
                continue
            seen.add(word)
            words.append(word)
    return tuple(words)


def should_submit_word(existing_status: Optional[int], target_status: str) -> bool:
    if existing_status is None:
        return True
    return existing_status < target_status_value(target_status)


def target_status_value(status: str) -> int:
    if status == "known":
        return KIMCHI_STATUS_KNOWN
    return KIMCHI_STATUS_SEEN


def create_artifact_dir(artifacts_root: Path) -> Path:
    artifacts_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = artifacts_root / timestamp
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def _counter_dict(counter: Counter) -> Dict[str, int]:
    return dict((key, int(counter[key])) for key in sorted(counter))


def build_report(
    plan: SyncPlan,
    steps: Tuple[ImportStepResult, ...],
    destination: Path,
) -> Dict[str, object]:
    deck_counter = Counter(entry.deck_name for entry in plan.candidates)
    model_counter = Counter(entry.model_name for entry in plan.candidates)
    skip_counter = Counter(item.reason for item in plan.skipped)
    planning_skip_counter = Counter(item.reason for item in plan.planning_skips)

    return {
        "generated_at": datetime.now().isoformat(),
        "selected_decks": list(plan.selected_decks),
        "artifact_dir": str(destination),
        "counts": {
            "candidate_cards": len(plan.candidates),
            "candidate_expressions": len(set(entry.expression for entry in plan.candidates)),
            "seen_expressions": len(plan.seen_expressions),
            "known_expressions": len(plan.known_expressions),
            "skipped_cards": len(plan.skipped),
            "planning_skipped_expressions": len(plan.planning_skips),
        },
        "by_deck_candidate_cards": _counter_dict(deck_counter),
        "by_model_candidate_cards": _counter_dict(model_counter),
        "skip_reasons": _counter_dict(skip_counter),
        "planning_skip_reasons": _counter_dict(planning_skip_counter),
        "import": {
            "mode": "sync",
            "steps": [asdict(step) for step in steps],
        },
    }


def write_sync_artifacts(
    plan: SyncPlan,
    steps: Tuple[ImportStepResult, ...],
    artifacts_root: Path,
) -> Path:
    destination = create_artifact_dir(artifacts_root)
    destination.joinpath("seen.txt").write_text(
        "\n".join(plan.seen_expressions) + ("\n" if plan.seen_expressions else ""),
        encoding="utf-8",
    )
    destination.joinpath("known.txt").write_text(
        "\n".join(plan.known_expressions) + ("\n" if plan.known_expressions else ""),
        encoding="utf-8",
    )

    submitted_seen = ()
    submitted_known = ()
    for step in steps:
        if step.status == "seen":
            submitted_seen = step.submitted_words
        elif step.status == "known":
            submitted_known = step.submitted_words

    destination.joinpath("submitted_seen.txt").write_text(
        "\n".join(submitted_seen) + ("\n" if submitted_seen else ""),
        encoding="utf-8",
    )
    destination.joinpath("submitted_known.txt").write_text(
        "\n".join(submitted_known) + ("\n" if submitted_known else ""),
        encoding="utf-8",
    )

    report = build_report(plan, steps, destination)
    destination.joinpath("report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


class CollectionSnapshotSource:
    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir

    def get_cards(self, deck_names: Sequence[str]) -> List[SourceCard]:
        with self._open_snapshot() as con:
            decks = self._load_decks(con)
            models = self._load_models(con)
            deck_name_to_id = dict(
                (deck["name"], int(deck_id)) for deck_id, deck in decks.items()
            )
            missing = [name for name in deck_names if name not in deck_name_to_id]
            if missing:
                raise KimchiAddonError(
                    "Selected deck(s) were not found in this collection: "
                    + ", ".join(missing)
                )

            deck_ids = [deck_name_to_id[name] for name in deck_names]
            placeholders = ",".join("?" for _ in deck_ids)
            rows = con.execute(
                """
                select c.id as cid, c.nid, c.did, c.reps, c.ivl, n.mid, n.flds
                from cards c
                join notes n on n.id = c.nid
                where c.did in ({})
                order by c.did, c.id
                """.format(placeholders),
                deck_ids,
            ).fetchall()

            cards = []
            for row in rows:
                model = models.get(str(row["mid"]))
                if model is None:
                    continue
                field_names = [field["name"] for field in model["flds"]]
                field_values = row["flds"].split("\x1f")
                cards.append(
                    SourceCard(
                        card_id=int(row["cid"]),
                        note_id=int(row["nid"]),
                        deck_name=decks[str(row["did"])]["name"],
                        model_name=model["name"],
                        fields=dict(zip(field_names, field_values)),
                        interval=int(row["ivl"]),
                        reps=int(row["reps"]),
                    )
                )
            return cards

    @contextmanager
    def _open_snapshot(self) -> Iterator[sqlite3.Connection]:
        collection_path = self._collection_path()
        source = self._open_readonly_source(collection_path)
        temp_dir = Path(tempfile.mkdtemp(prefix="anki_kimchi_sync_snapshot_"))
        snapshot_path = temp_dir / collection_path.name
        snapshot = sqlite3.connect(str(snapshot_path))
        snapshot.row_factory = sqlite3.Row
        try:
            source.backup(snapshot)
            snapshot.commit()
            yield snapshot
        finally:
            snapshot.close()
            source.close()
            shutil.rmtree(str(temp_dir), ignore_errors=True)

    def _collection_path(self) -> Path:
        for file_name in COLLECTION_FILE_NAMES:
            candidate = self.profile_dir / file_name
            if candidate.exists():
                return candidate
        raise KimchiAddonError(
            "Could not find collection.anki21 or collection.anki2 in %s."
            % self.profile_dir
        )

    @staticmethod
    def _open_readonly_source(collection_path: Path) -> sqlite3.Connection:
        resolved = collection_path.resolve()
        try:
            return sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
        except (sqlite3.Error, ValueError):
            pass

        uri = "file:%s?mode=ro" % quote(resolved.as_posix(), safe="/:")
        try:
            return sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return sqlite3.connect(str(resolved))

    @staticmethod
    def _load_decks(con: sqlite3.Connection) -> Dict[str, Dict[str, object]]:
        row = con.execute("select decks from col").fetchone()
        if row is not None and row["decks"]:
            return json.loads(row["decks"])

        return dict(
            (
                str(result["id"]),
                {"name": result["name"]},
            )
            for result in con.execute("select id, name from decks").fetchall()
        )

    @staticmethod
    def _load_models(con: sqlite3.Connection) -> Dict[str, Dict[str, object]]:
        row = con.execute("select models from col").fetchone()
        if row is not None and row["models"]:
            return json.loads(row["models"])

        models = dict(
            (
                str(result["id"]),
                {"name": result["name"], "flds": []},
            )
            for result in con.execute("select id, name from notetypes").fetchall()
        )
        for field_row in con.execute(
            "select ntid, ord, name from fields order by ntid, ord"
        ).fetchall():
            model = models.setdefault(
                str(field_row["ntid"]),
                {"name": str(field_row["ntid"]), "flds": []},
            )
            model["flds"].append(
                {"name": field_row["name"], "ord": field_row["ord"]}
            )
        return models


def choose_field(card: SourceCard, settings: SyncSettings) -> Tuple[Optional[str], Tuple[str, ...]]:
    field_names = tuple(card.fields.keys())
    candidate_names = []

    for name in settings.model_field_overrides.get(card.model_name, ()):
        if name in card.fields and name not in candidate_names:
            candidate_names.append(name)

    for name in settings.explicit_field_priority:
        if name in card.fields and name not in candidate_names:
            candidate_names.append(name)

    for name in settings.generic_field_priority:
        if name in card.fields and name not in candidate_names:
            candidate_names.append(name)

    if not candidate_names:
        return None, field_names

    for name in candidate_names:
        cleaned = strip_markup(card.fields.get(name, ""))
        if cleaned:
            return name, tuple(candidate_names)

    return candidate_names[0], tuple(candidate_names)


def extract_candidate(card: SourceCard, settings: SyncSettings) -> Tuple[Optional[CandidateEntry], Optional[SkippedEntry]]:
    field_name, considered_fields = choose_field(card, settings)
    if field_name is None:
        return None, SkippedEntry(
            reason="no_safe_field",
            message="No supported Korean source field was found for this model.",
            deck_name=card.deck_name,
            model_name=card.model_name,
            card_id=card.card_id,
            note_id=card.note_id,
            fields_considered=considered_fields,
        )

    source_text = card.fields.get(field_name, "")
    cleaned = strip_markup(source_text)
    if not cleaned:
        return None, SkippedEntry(
            reason="empty_field",
            message="Selected field '%s' was empty after markup stripping." % field_name,
            deck_name=card.deck_name,
            model_name=card.model_name,
            card_id=card.card_id,
            note_id=card.note_id,
            fields_considered=considered_fields,
            source_text=source_text,
        )

    tokens = extract_korean_tokens(cleaned)
    if not tokens:
        return None, SkippedEntry(
            reason="no_korean_text",
            message="Selected field '%s' did not contain Korean text." % field_name,
            deck_name=card.deck_name,
            model_name=card.model_name,
            card_id=card.card_id,
            note_id=card.note_id,
            fields_considered=considered_fields,
            source_text=cleaned,
        )

    if len(tokens) > settings.max_korean_tokens:
        return None, SkippedEntry(
            reason="too_many_tokens",
            message=(
                "Selected field '%s' contains %d Korean tokens, so it looks sentence-like and was skipped."
                % (field_name, len(tokens))
            ),
            deck_name=card.deck_name,
            model_name=card.model_name,
            card_id=card.card_id,
            note_id=card.note_id,
            fields_considered=considered_fields,
            source_text=cleaned,
        )

    return CandidateEntry(
        expression=" ".join(tokens),
        source_text=source_text,
        normalized_source=cleaned,
        selected_field=field_name,
        deck_name=card.deck_name,
        model_name=card.model_name,
        card_id=card.card_id,
        note_id=card.note_id,
        interval=card.interval,
        reps=card.reps,
        token_count=len(tokens),
    ), None


def collect_candidates(
    cards: Sequence[SourceCard],
    settings: SyncSettings,
) -> Tuple[List[CandidateEntry], List[SkippedEntry]]:
    candidates = []
    skipped = []

    for card in cards:
        candidate, skipped_entry = extract_candidate(card, settings)
        if candidate is not None:
            candidates.append(candidate)
        elif skipped_entry is not None:
            skipped.append(skipped_entry)

    return candidates, skipped


def plan_statuses(
    candidates: Sequence[CandidateEntry],
    skipped: Sequence[SkippedEntry],
    selected_decks: Sequence[str],
    settings: SyncSettings,
) -> SyncPlan:
    grouped = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.expression].append(candidate)

    planned = []
    planning_skips = []

    for expression, entries in sorted(grouped.items()):
        entries = sorted(
            entries,
            key=lambda item: (item.deck_name, item.model_name, item.card_id),
        )
        if any(
            entry.reps > 0 and entry.interval >= settings.mature_interval_days
            for entry in entries
        ):
            planned.append(
                PlannedExpression(
                    expression=expression,
                    status="known",
                    entries=tuple(entries),
                )
            )
            continue

        if any(entry.reps > 0 for entry in entries):
            planned.append(
                PlannedExpression(
                    expression=expression,
                    status="seen",
                    entries=tuple(entries),
                )
            )
            continue

        planning_skips.append(
            PlannedExpressionSkip(
                expression=expression,
                reason="unstudied_only",
                entries=tuple(entries),
            )
        )

    planned.sort(key=lambda item: (item.status, item.expression))
    return SyncPlan(
        selected_decks=tuple(selected_decks),
        candidates=tuple(candidates),
        skipped=tuple(skipped),
        planned=tuple(planned),
        planning_skips=tuple(planning_skips),
    )


class LocalStateStore:
    def __init__(self, user_files_dir: Path):
        self.user_files_dir = user_files_dir
        self.state_path = self.user_files_dir / STATE_FILE_NAME

    def load(self) -> Dict[str, object]:
        self.user_files_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.state_path.write_text("{}\n", encoding="utf-8")
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(payload, dict):
            return payload
        return {}

    def save(self, payload: Dict[str, object]) -> None:
        self.user_files_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def get(self, key: str, default: object = None) -> object:
        state = self.load()
        return state.get(key, default)

    def set(self, key: str, value: object) -> None:
        state = self.load()
        state[key] = value
        self.save(state)


class KimchiAuthManager:
    def __init__(self, user_files_dir: Path):
        self.store = LocalStateStore(user_files_dir)

    def get_tokens(self) -> AuthTokens:
        raw = self.store.get("auth:tokens")
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
        elif isinstance(raw, dict):
            payload = raw
        else:
            payload = {}
        return AuthTokens(
            access_token=payload.get("access_token"),
            refresh_token=payload.get("refresh_token"),
        )

    def set_tokens(self, tokens: AuthTokens) -> None:
        payload = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
        }
        self.store.set("auth:tokens", json.dumps(payload, ensure_ascii=False))

    def clear_tokens(self) -> None:
        self.set_tokens(AuthTokens(access_token=None, refresh_token=None))

    def set_code_verifier(self, verifier: str) -> None:
        self.store.set("auth:pkce", verifier)

    def get_code_verifier(self) -> Optional[str]:
        value = self.store.get("auth:pkce")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def clear_code_verifier(self) -> None:
        self.store.set("auth:pkce", "")

    def current_email(self) -> Optional[str]:
        access_token = self.get_tokens().access_token
        payload = decode_jwt_payload(access_token)
        email = payload.get("email") if payload else None
        if isinstance(email, str) and email.strip():
            return email.strip()
        return None

    def ensure_valid_access_token(self) -> Optional[str]:
        tokens = self.get_tokens()
        now = datetime.now(timezone.utc)
        expires_at = token_expiry(tokens.access_token)

        if tokens.access_token and expires_at and expires_at > now + TOKEN_REFRESH_WINDOW:
            return tokens.access_token
        if tokens.access_token and expires_at is None and not tokens.refresh_token:
            return tokens.access_token

        refreshed = None
        if tokens.refresh_token:
            refreshed = self.refresh_tokens()
        if refreshed and refreshed.access_token:
            return refreshed.access_token

        if tokens.access_token and expires_at and expires_at > now:
            return tokens.access_token

        return None

    def request_email_otp(self, email: str) -> None:
        verifier = generate_code_verifier()
        challenge = create_code_challenge(verifier)
        payload = {
            "email": email.strip().lower(),
            "code_challenge": challenge,
        }
        response = http_json(
            method="POST",
            url="%s/auth/otp" % KIMCHI_AUTH_API_BASE_URL,
            payload=payload,
            expected_statuses=(204,),
            allow_empty_response=True,
        )
        del response
        self.set_code_verifier(verifier)

    def verify_email_otp(self, email: str, auth_code: str) -> None:
        verifier = self.get_code_verifier()
        if not verifier:
            raise KimchiAddonError(
                "The saved login challenge was missing. Request a new code and try again."
            )
        payload = {
            "email": email.strip().lower(),
            "auth_code": auth_code.strip(),
            "code_verifier": verifier,
        }
        response = http_json(
            method="POST",
            url="%s/auth/otp/verify" % KIMCHI_AUTH_API_BASE_URL,
            payload=payload,
            expected_statuses=(200,),
        )
        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token")
        if not access_token or not refresh_token:
            raise KimchiAddonError("Kimchi login succeeded but did not return both tokens.")
        self.set_tokens(
            AuthTokens(
                access_token=str(access_token),
                refresh_token=str(refresh_token),
            )
        )
        self.clear_code_verifier()

    def refresh_tokens(self) -> Optional[AuthTokens]:
        tokens = self.get_tokens()
        if not tokens.refresh_token:
            return None

        response, status = http_json_with_status(
            method="POST",
            url="%s/auth/token/refresh" % KIMCHI_AUTH_API_BASE_URL,
            payload={"refresh_token": tokens.refresh_token},
            expected_statuses=(200, 401),
        )
        if status == 401:
            self.clear_tokens()
            return None

        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token")
        if not access_token or not refresh_token:
            raise KimchiAddonError("Kimchi returned an incomplete token refresh response.")

        refreshed = AuthTokens(
            access_token=str(access_token),
            refresh_token=str(refresh_token),
        )
        self.set_tokens(refreshed)
        return refreshed


def generate_code_verifier() -> str:
    return secrets.token_hex(56)


def create_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def decode_jwt_payload(token: Optional[str]) -> Optional[Dict[str, object]]:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        return data
    return None


def token_expiry(token: Optional[str]) -> Optional[datetime]:
    payload = decode_jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(float(exp), timezone.utc)


def http_json(
    method: str,
    url: str,
    payload: Optional[object] = None,
    access_token: Optional[str] = None,
    expected_statuses: Tuple[int, ...] = (200,),
    allow_empty_response: bool = False,
) -> Dict[str, object]:
    data, _ = http_json_with_status(
        method=method,
        url=url,
        payload=payload,
        access_token=access_token,
        expected_statuses=expected_statuses,
        allow_empty_response=allow_empty_response,
    )
    return data


def http_json_with_status(
    method: str,
    url: str,
    payload: Optional[object] = None,
    access_token: Optional[str] = None,
    expected_statuses: Tuple[int, ...] = (200,),
    allow_empty_response: bool = False,
) -> Tuple[Dict[str, object], int]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "anki-kimchi-reader-sync/1.0",
        "Origin": KIMCHI_ORIGIN,
        "Referer": KIMCHI_ORIGIN + "/",
    }
    data = None
    if access_token:
        headers["Authorization"] = "Bearer %s" % access_token
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    http_request = request.Request(url, data=data, headers=headers, method=method)

    try:
        with request.urlopen(http_request, timeout=60) as response:
            status = response.getcode()
            response_text = response.read().decode("utf-8")
    except error.HTTPError as exc:
        status = exc.code
        response_text = exc.read().decode("utf-8", errors="replace")
        if status not in expected_statuses:
            detail = response_text.strip() or exc.reason
            raise KimchiAddonError(
                "Kimchi request failed with HTTP %s for %s: %s"
                % (status, url, detail)
            )
    except error.URLError as exc:
        raise KimchiAddonError("Could not reach Kimchi at %s: %s" % (url, exc.reason))

    if status not in expected_statuses:
        raise KimchiAddonError(
            "Kimchi request returned unexpected HTTP %s for %s."
            % (status, url)
        )

    if not response_text.strip():
        if allow_empty_response:
            return {}, status
        return {}, status

    try:
        payload = json.loads(response_text)
    except ValueError:
        return {"text": response_text}, status

    if isinstance(payload, dict):
        return payload, status
    if isinstance(payload, list):
        return {"items": payload}, status
    return {"value": payload}, status


class KimchiSyncBackend:
    def __init__(
        self,
        profile_dir: Path,
        user_files_dir: Path,
        settings: Optional[SyncSettings] = None,
    ):
        self.profile_dir = profile_dir
        self.user_files_dir = user_files_dir
        self.settings = settings or SyncSettings()
        self.auth = KimchiAuthManager(user_files_dir)
        self.invalid_word_cache_path = user_files_dir / INVALID_WORD_CACHE_FILE_NAME
        self.artifacts_root = user_files_dir / RUNS_DIR_NAME

    def authenticate_with_otp(self, email: str, auth_code: str) -> None:
        self.auth.verify_email_otp(email, auth_code)

    def request_email_otp(self, email: str) -> None:
        self.auth.request_email_otp(email)

    def ensure_authenticated(self) -> bool:
        return self.auth.ensure_valid_access_token() is not None

    def current_email(self) -> Optional[str]:
        return self.auth.current_email()

    def prepare_sync(self, selected_decks: Sequence[str]) -> SyncPreview:
        access_token = self.auth.ensure_valid_access_token()
        if not access_token:
            raise KimchiAddonError("Kimchi login is required before syncing.")

        plan = self._build_plan(selected_decks)
        current_statuses = self._load_current_statuses(access_token)
        invalid_word_cache = self._load_invalid_word_cache()
        steps = []

        for status, expressions in (
            ("seen", plan.seen_expressions),
            ("known", plan.known_expressions),
        ):
            if not expressions:
                steps.append(
                    PreviewStepResult(
                        status=status,
                        expression_count=0,
                        resolved_word_count=0,
                        already_marked_count=0,
                        cached_invalid_count=0,
                        details="No expressions to import.",
                        pending_words=(),
                    )
                )
                continue

            words = self._resolve_words(expressions, access_token)
            filtered_words = self._filter_existing_words(words, status, current_statuses)
            skipped_existing = len(words) - len(filtered_words)
            filtered_invalid_cache = self._filter_invalid_cache_words(
                filtered_words,
                invalid_word_cache,
            )
            skipped_cached_invalid = len(filtered_words) - len(filtered_invalid_cache)

            if not filtered_invalid_cache:
                steps.append(
                    PreviewStepResult(
                        status=status,
                        expression_count=len(expressions),
                        resolved_word_count=len(words),
                        already_marked_count=skipped_existing,
                        cached_invalid_count=skipped_cached_invalid,
                        details=(
                            "No Kimchi words need submission for this batch. "
                            "Filtered out %d already-marked word(s) and %d cached invalid word(s)."
                            % (skipped_existing, skipped_cached_invalid)
                        ),
                        pending_words=(),
                    )
                )
                continue

            steps.append(
                PreviewStepResult(
                    status=status,
                    expression_count=len(expressions),
                    resolved_word_count=len(words),
                    already_marked_count=skipped_existing,
                    cached_invalid_count=skipped_cached_invalid,
                    details=(
                        "Lemmatized %d expression(s) to %d Kimchi word(s), filtered out %d already-marked word(s), "
                        "filtered out %d cached invalid word(s), and prepared %d pending word(s)."
                        % (
                            len(expressions),
                            len(words),
                            skipped_existing,
                            skipped_cached_invalid,
                            len(filtered_invalid_cache),
                        )
                    ),
                    pending_words=tuple(filtered_invalid_cache),
                )
            )

        return SyncPreview(
            selected_decks=tuple(selected_decks),
            plan=plan,
            steps=tuple(steps),
        )

    def run_prepared_sync(self, preview: SyncPreview) -> SyncRunResult:
        access_token = self.auth.ensure_valid_access_token()
        if not access_token:
            raise KimchiAddonError("Kimchi login is required before syncing.")

        plan = preview.plan
        current_statuses = self._load_current_statuses(access_token)
        invalid_word_cache = self._load_invalid_word_cache()
        steps = []

        for preview_step in preview.steps:
            status = preview_step.status
            words = self._filter_existing_words(
                preview_step.pending_words,
                status,
                current_statuses,
            )
            skipped_existing = len(preview_step.pending_words) - len(words)
            filtered_invalid_cache = self._filter_invalid_cache_words(
                words,
                invalid_word_cache,
            )
            skipped_cached_invalid = len(words) - len(filtered_invalid_cache)

            if not filtered_invalid_cache:
                steps.append(
                    ImportStepResult(
                        status=status,
                        submitted_count=0,
                        success=True,
                        details=(
                            "No Kimchi words needed submission after confirmation. "
                            "Filtered out %d already-marked word(s) and %d cached invalid word(s)."
                            % (skipped_existing, skipped_cached_invalid)
                        ),
                        submitted_words=(),
                    )
                )
                continue

            invalid_words = self._submit_status_updates(
                filtered_invalid_cache,
                status,
                access_token,
            )
            invalid_word_cache.update(invalid_words)
            invalid_word_set = set(invalid_words)
            submitted_words = tuple(
                word for word in filtered_invalid_cache if word not in invalid_word_set
            )
            submitted_count = len(submitted_words)
            for word in submitted_words:
                current_statuses[word] = target_status_value(status)

            steps.append(
                ImportStepResult(
                    status=status,
                    submitted_count=submitted_count,
                    success=True,
                    details=(
                        "Submitted %d confirmed word(s), skipped %d invalid word(s), and filtered out %d word(s) "
                        "that no longer needed changes before submission."
                        % (
                            submitted_count,
                            len(invalid_words),
                            skipped_existing + skipped_cached_invalid,
                        )
                    ),
                    submitted_words=submitted_words,
                )
            )

        self._save_invalid_word_cache(invalid_word_cache)
        artifacts_dir = write_sync_artifacts(plan, tuple(steps), self.artifacts_root)
        return SyncRunResult(
            selected_decks=preview.selected_decks,
            plan=plan,
            steps=tuple(steps),
            artifacts_dir=str(artifacts_dir),
        )

    def run_sync(self, selected_decks: Sequence[str]) -> SyncRunResult:
        preview = self.prepare_sync(selected_decks)
        return self.run_prepared_sync(preview)

    def _build_plan(self, selected_decks: Sequence[str]) -> SyncPlan:
        source = CollectionSnapshotSource(self.profile_dir)
        cards = source.get_cards(selected_decks)
        candidates, skipped = collect_candidates(cards, self.settings)
        return plan_statuses(candidates, skipped, selected_decks, self.settings)

    def _resolve_words(
        self,
        expressions: Sequence[str],
        access_token: str,
    ) -> Tuple[str, ...]:
        words = []
        seen = set()
        chunks = tuple(self._chunked(expressions, self.settings.kimchi_lemmatize_batch_size))

        for chunk in chunks:
            payload, _ = http_json_with_status(
                method="POST",
                url="%s/v2/lemmatize" % KIMCHI_API_BASE_URL,
                payload=list(chunk),
                access_token=access_token,
                expected_statuses=(200,),
            )
            response_items = payload.get("items", [])
            for word in extract_words_from_lemmatize_response(response_items):
                if word in seen:
                    continue
                seen.add(word)
                words.append(word)

        return tuple(words)

    def _load_current_statuses(self, access_token: str) -> Dict[str, int]:
        statuses = {}
        cursor = None

        while True:
            query = {"limit": str(KIMCHI_HISTORY_PAGE_LIMIT)}
            if cursor is not None:
                query["from_created_at"] = cursor

            url = "%s/v2/word/status/history?%s" % (
                KIMCHI_API_BASE_URL,
                parse.urlencode(query),
            )
            payload, _ = http_json_with_status(
                method="GET",
                url=url,
                access_token=access_token,
                expected_statuses=(200,),
            )
            items = payload.get("items", [])
            if not isinstance(items, list) or not items:
                break

            last_created_at = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                word = str(item.get("word") or "").strip()
                created_at = str(item.get("created_at") or "").strip()
                try:
                    status_value = int(item.get("status"))
                except (TypeError, ValueError):
                    continue
                if word:
                    statuses[word] = status_value
                if created_at:
                    last_created_at = created_at

            if len(items) < KIMCHI_HISTORY_PAGE_LIMIT or not last_created_at:
                break
            cursor = self._increment_timestamp(last_created_at)

        return statuses

    @staticmethod
    def _filter_existing_words(
        words: Sequence[str],
        target_status: str,
        current_statuses: Dict[str, int],
    ) -> Tuple[str, ...]:
        return tuple(
            word
            for word in words
            if should_submit_word(current_statuses.get(word), target_status)
        )

    @staticmethod
    def _filter_invalid_cache_words(
        words: Sequence[str],
        invalid_word_cache: Iterable[str],
    ) -> Tuple[str, ...]:
        invalid_set = set(invalid_word_cache)
        return tuple(word for word in words if word not in invalid_set)

    def _submit_status_updates(
        self,
        words: Sequence[str],
        status: str,
        access_token: str,
    ) -> Tuple[str, ...]:
        invalid_words = []
        status_value = target_status_value(status)
        chunks = tuple(self._chunked(words, self._status_batch_size()))

        for chunk in chunks:
            invalid_words.extend(
                self._submit_status_chunk(
                    chunk=chunk,
                    payload_status=status_value,
                    access_token=access_token,
                )
            )
        return tuple(invalid_words)

    def _submit_status_chunk(
        self,
        chunk: Tuple[str, ...],
        payload_status: int,
        access_token: str,
    ) -> Tuple[str, ...]:
        body = [
            {
                "word": word,
                "created_at": self._created_at(),
                "status": payload_status,
            }
            for word in chunk
        ]

        try:
            response, _ = http_json_with_status(
                method="POST",
                url="%s/v2/word/status/bulk" % KIMCHI_API_BASE_URL,
                payload=body,
                access_token=access_token,
                expected_statuses=(200,),
            )
        except KimchiAddonError as exc:
            message = str(exc)
            if "HTTP 400" in message and "Not a valid word" in message:
                if len(chunk) == 1:
                    return chunk
                midpoint = max(1, len(chunk) // 2)
                left = self._submit_status_chunk(
                    chunk[:midpoint],
                    payload_status,
                    access_token,
                )
                right = self._submit_status_chunk(
                    chunk[midpoint:],
                    payload_status,
                    access_token,
                )
                return left + right
            raise

        if isinstance(response, dict) and response.get("status") == 1:
            return ()

        raise KimchiAddonError(
            "Kimchi bulk status update did not return the expected success payload: %r"
            % response
        )

    def _load_invalid_word_cache(self) -> set:
        if not self.invalid_word_cache_path.exists():
            return set()
        try:
            payload = json.loads(
                self.invalid_word_cache_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return set()
        if not isinstance(payload, list):
            return set()
        return set(str(item).strip() for item in payload if str(item).strip())

    def _save_invalid_word_cache(self, invalid_word_cache: Iterable[str]) -> None:
        self.user_files_dir.mkdir(parents=True, exist_ok=True)
        payload = sorted(set(invalid_word_cache))
        self.invalid_word_cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _status_batch_size(self) -> int:
        configured = self.settings.kimchi_status_batch_size
        if configured <= 0:
            return KIMCHI_STATUS_BULK_LIMIT
        return min(configured, KIMCHI_STATUS_BULK_LIMIT)

    @staticmethod
    def _created_at() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00",
            "Z",
        )

    @staticmethod
    def _increment_timestamp(value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed + timedelta(milliseconds=1)).astimezone(
            timezone.utc
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _chunked(values: Sequence[str], size: int) -> Iterator[Tuple[str, ...]]:
        chunk = []
        for value in values:
            chunk.append(value)
            if len(chunk) >= size:
                yield tuple(chunk)
                chunk = []
        if chunk:
            yield tuple(chunk)
