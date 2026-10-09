from jobagent.config import Profile
from jobagent.store import Job


def make_profile(**over) -> Profile:
    base = {
        "target_roles": ["Backend Engineer"],
        "skills": ["python", "sql"],
        "experience_years": 3,
        "locations": ["Bengaluru", "Remote"],
        "countries": ["IN"],
        "min_score": 0,
    }
    base.update(over)
    return Profile.model_validate(base)


def make_job(**over) -> Job:
    base = {
        "id": 1, "board_id": 1, "external_id": "1", "title": "Backend Engineer",
        "company": "Acme", "department": None, "location_text": "Bangalore, India",
        "cities": ["Bengaluru"], "countries": ["IN"], "is_remote": False,
        "description": "Python and SQL. 3 years.", "apply_url": "https://x.example/1",
        "posted_at": None, "content_hash": "h", "first_seen_at": "2026-01-01",
    }  # fmt: skip
    base.update(over)
    return Job(**base)


def gh_job(id_=1, title="Backend Engineer", loc="Bangalore, India", content="<p>Python</p>", **o):
    d = {
        "id": id_, "title": title, "location": {"name": loc},
        "absolute_url": f"https://boards.example/acme/jobs/{id_}",
        "updated_at": "2026-05-01T10:00:00-04:00", "content": content,
        "departments": [{"name": "Eng"}],
    }  # fmt: skip
    d.update(o)
    return d
