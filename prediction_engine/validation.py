"""Validate feed rows without writing files or making network calls."""

import math
import re
from datetime import datetime, timezone


LEAGUES = {"esb8": 8, "volta6": 6, "h2hgg": 8, "gt12": 12}


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include its timezone")
    return parsed.astimezone(timezone.utc)


def identity(row):
    if row.get("fixture_id"):
        return str(row["fixture_id"])
    # Players and kickoff identify a game; Forebet sometimes relabels a club
    # (Crysis appeared as both "FC Salzburg" and "RB Salzburg"), which must not
    # turn one result into two.
    return tuple(row[key].casefold().strip() for key in ("home_tag", "away_tag", "kickoff_utc"))


def clean_rows(rows):
    found, errors, conflicts = {}, [], set()
    if not isinstance(rows, list):
        return [], ["league_unavailable"]
    for index, original in enumerate(rows):
        try:
            if not isinstance(original, dict):
                raise ValueError("row must be an object")
            row = dict(original)
            for key in ("home_tag", "away_tag", "home_club", "away_club"):
                if not isinstance(row[key], str) or not row[key].strip():
                    raise ValueError(f"invalid {key}")
            row["kickoff_utc"] = utc(row["kickoff_utc"]).isoformat()
            if row.get("result_seen_at"):
                row["result_seen_at"] = utc(row["result_seen_at"]).isoformat()
            if row.get("status") == "played":
                match = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", row.get("actual_score") or "")
                if not match or max(map(int, match.groups())) > 40:
                    raise ValueError("invalid final score")
                row["actual_score"] = "-".join(str(int(value)) for value in match.groups())
            elif row.get("status") in {"upcoming", "live"}:
                if row.get("actual_score"):
                    raise ValueError("unconfirmed final result on unfinished fixture")
                if row["status"] == "live" and row.get("live_score") is not None:
                    score = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", row["live_score"])
                    if not score or max(map(int, score.groups())) > 40:
                        raise ValueError("invalid live score")
                numbers = [row[key] for key in ("p1", "px", "p2")]
                if any(not isinstance(p, (int, float)) or isinstance(p, bool)
                       or not math.isfinite(p) or p < 0 or p > 100 for p in numbers):
                    raise ValueError("invalid probabilities")
                if abs(sum(numbers) - 100) > 2:
                    raise ValueError("probabilities do not sum to 100")
            else:
                raise ValueError("unrecognized fixture status")
            key = identity(row)
            previous = found.get(key)
            if previous and previous.get("status") == "played":
                if row["status"] == "played" and previous["actual_score"] != row["actual_score"]:
                    conflicts.add(key)
                continue
            found[key] = row
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            errors.append(f"row {index}: {exc}")
    for key in conflicts:
        del found[key]
        errors.append("conflicting final scores: fixture excluded")
    return sorted(found.values(), key=lambda row: row["kickoff_utc"]), errors
