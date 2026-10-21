"""The "name the unknown numbers" loop: list_unnamed_contacts -> set_contact_names.

Names land in contact_nicknames (the table manage_nickname writes) and must show
up at once wherever the MCP prints a name, without waiting for the bridge to
rebuild the identity directory.
"""

import importlib
import sqlite3

import pytest

import whatsapp
from lib import identity, naming


def _contacts(**kwargs):
    result = naming.list_unnamed_contacts(**kwargs)
    assert result["available"] is True
    return {c["jid"]: c for c in result["contacts"]}, result


def _nicknames(db: str) -> dict[str, str]:
    with sqlite3.connect(db) as conn:
        return dict(conn.execute("SELECT jid, nickname FROM contact_nicknames").fetchall())


# --- list_unnamed_contacts ---------------------------------------------------


def test_lists_only_unnamed_people_ranked_by_recent_activity(naming_store):
    by_jid, result = _contacts()

    # Jane has an address-book name; the linked account is never listed.
    assert "441234@s.whatsapp.net" not in by_jid
    assert "385000@s.whatsapp.net" not in by_jid
    assert "120999@g.us" not in by_jid  # groups are opt-in
    assert [c["jid"] for c in result["contacts"]] == [
        "222@lid",
        "385111@s.whatsapp.net",
        "385333@s.whatsapp.net",
    ]
    assert result["total_unnamed"] == 3


def test_entry_carries_identity_activity_and_context(naming_store):
    by_jid, _ = _contacts()

    ivo = by_jid["385111@s.whatsapp.net"]
    assert ivo["phone"] == "385111"
    assert ivo["lid"] == "111@lid"
    assert ivo["push_name"] == "Ivo"
    assert ivo["message_count"] == 2
    assert ivo["last_active"] == "2026-10-20T07:05:00Z"  # UTC
    assert [(m["from"], m["where"]) for m in ivo["recent_messages"]] == [("me", "direct"), ("them", "direct")]
    assert ivo["recent_messages"][1]["text"] == "Hi, this is Ivo from the marina"

    # Group-only LID sender: activity counted under bare and @lid sender forms.
    lid_only = by_jid["222@lid"]
    assert lid_only["phone"] is None and lid_only["push_name"] is None
    assert lid_only["message_count"] == 3
    assert lid_only["shared_groups"] == ["Boat Club"]
    texts = [m["text"] for m in lid_only["recent_messages"]]
    assert texts[0] == "Reminder about the berth fee " + "x" * 90 + "…"  # empty b3 is skipped
    assert all(len(t) <= naming.SNIPPET_MAX_CHARS for t in texts)
    assert lid_only["recent_messages"][0]["where"] == "Boat Club"


def test_min_messages_and_limit(naming_store):
    by_jid, _ = _contacts(min_messages=2)
    assert set(by_jid) == {"222@lid", "385111@s.whatsapp.net"}

    _, result = _contacts(limit=1)
    assert result["count"] == 1
    assert result["total_unnamed"] == 3

    _, capped = _contacts(limit=10_000)
    assert capped["count"] <= naming.UNNAMED_MAX_LIMIT


def test_include_groups_lists_groups_without_a_subject(naming_store):
    by_jid, _ = _contacts(include_groups=True)
    assert "120999@g.us" in by_jid
    assert "120363@g.us" not in by_jid  # has a subject
    group = by_jid["120999@g.us"]
    assert group["kind"] == "group"
    assert group["message_count"] == 1
    assert "push_name" not in group


def test_directory_missing_degrades_to_empty(naming_store):
    with sqlite3.connect(naming_store) as conn:
        conn.execute("DELETE FROM identities")
    result = naming.list_unnamed_contacts()
    assert result["available"] is False
    assert result["contacts"] == []


# --- set_contact_names -------------------------------------------------------


def test_set_names_stores_under_the_canonical_jid(naming_store):
    result = naming.set_contact_names(
        [{"jid": "111@lid", "name": "Ivo Marina"}, {"jid": "222", "name": "  Club\tTreasurer\n"}],
        confirmed_by_user=True,
    )
    assert result["success"] is True and result["saved"] == 2
    first, second = result["results"]
    assert first == {
        "jid": "385111@s.whatsapp.net",
        "success": True,
        "name": "Ivo Marina",
        "source": "user",
        "input": "111@lid",
    }
    assert second["jid"] == "222@lid" and second["name"] == "Club Treasurer"
    assert _nicknames(naming_store) == {"385111@s.whatsapp.net": "Ivo Marina", "222@lid": "Club Treasurer"}

    # Named now, so no longer listed.
    by_jid, _ = _contacts()
    assert set(by_jid) == {"385333@s.whatsapp.net"}


def test_set_names_validates_each_item(naming_store):
    result = naming.set_contact_names(
        [
            {"jid": "nobody@s.whatsapp.net", "name": "Ghost"},
            {"jid": "385111@s.whatsapp.net", "name": "\x00\x07 "},
            {"jid": "385111@s.whatsapp.net", "name": "x" * 65},
            {"name": "No Jid"},
            {"jid": "385333@s.whatsapp.net", "name": "Ana‮\x1b[31m"},
        ],
        confirmed_by_user=True,
    )
    errors = [r.get("error") for r in result["results"]]
    assert "unknown jid" in errors[0]
    assert "empty" in errors[1]
    assert "64" in errors[2]
    assert errors[3] == "jid is required"
    assert result["results"][4] == {
        "jid": "385333@s.whatsapp.net",
        "success": True,
        "name": "Ana[31m",
        "source": "user",
    }
    assert result["saved"] == 1 and result["failed"] == 4
    assert _nicknames(naming_store) == {"385333@s.whatsapp.net": "Ana[31m"}


def test_set_names_rejects_oversized_or_malformed_batches(naming_store):
    too_many = [{"jid": "385111@s.whatsapp.net", "name": f"N{i}"} for i in range(naming.SET_NAMES_MAX_ITEMS + 1)]
    assert naming.set_contact_names(too_many, confirmed_by_user=True)["success"] is False
    assert naming.set_contact_names([], confirmed_by_user=True)["success"] is False
    assert _nicknames(naming_store) == {}


def test_set_names_accepts_a_jid_known_only_as_a_chat(naming_store):
    with sqlite3.connect(naming_store) as conn:
        conn.execute("INSERT INTO chats (jid, name) VALUES ('777@s.whatsapp.net', '777')")
    result = naming.set_contact_names([{"jid": "777@s.whatsapp.net", "name": "Plumber"}], confirmed_by_user=True)
    assert result["results"][0]["success"] is True


def test_set_names_replaces_and_dedupes_jid_forms(naming_store):
    with sqlite3.connect(naming_store) as conn:
        conn.execute("INSERT INTO contact_nicknames (jid, nickname) VALUES ('111@lid', 'Old')")
    result = naming.set_contact_names([{"jid": "385111", "name": "Ivo"}], confirmed_by_user=True)
    assert result["results"][0]["previous"] == "Old"
    # The stale LID-keyed nickname is gone, so the bridge cannot pick it instead.
    assert _nicknames(naming_store) == {"385111@s.whatsapp.net": "Ivo"}


# --- names show up where agents look ------------------------------------------


def test_nickname_wins_in_directory_reads_before_bridge_rebuild(naming_store):
    naming.set_contact_names([{"jid": "385111@s.whatsapp.net", "name": "Ivo Marina"}], confirmed_by_user=True)

    found = identity.get_identity("111@lid")
    assert found is not None
    assert found.display_name == "Ivo Marina"
    assert found.nickname == "Ivo Marina"
    by_name = identity.get_identity("Ivo Marina")
    assert by_name is not None and by_name.jid == "385111@s.whatsapp.net"
    assert [i.jid for i in identity.list_identities(query="marina")] == ["385111@s.whatsapp.net"]
    # Untouched rows read exactly as the bridge wrote them.
    jane = identity.get_identity("441234")
    assert jane is not None and jane.display_name == "Jane Doe"


def test_removed_nickname_stops_showing_before_bridge_rebuild(naming_store):
    with sqlite3.connect(naming_store) as conn:
        conn.execute(
            "UPDATE identities SET nickname = 'Gone', display_name = 'Gone' WHERE jid = '385111@s.whatsapp.net'"
        )
    found = identity.get_identity("385111")
    assert found is not None
    assert found.nickname is None
    assert found.display_name == "385111"  # chat_name, as the bridge would resolve it


def test_list_chats_and_messages_use_nicknames(naming_store):
    naming.set_contact_names(
        [{"jid": "385111@s.whatsapp.net", "name": "Ivo Marina"}, {"jid": "222@lid", "name": "Treasurer"}],
        confirmed_by_user=True,
    )

    chats = {c["jid"]: c["name"] for c in whatsapp.list_chats(limit=50)}
    assert chats["385111@s.whatsapp.net"] == "Ivo Marina"
    assert chats["120363@g.us"] == "Boat Club"
    assert chats["441234@s.whatsapp.net"] == "Jane Doe"
    assert [c["jid"] for c in whatsapp.list_chats(query="marina")] == ["385111@s.whatsapp.net"]

    msgs = {m["id"]: m for m in whatsapp.list_messages(limit=50)}
    assert msgs["b1"]["sender_name"] == "Treasurer"  # bare LID user part
    assert msgs["b2"]["sender_name"] == "Treasurer"  # full @lid
    assert msgs["a1"]["sender_name"] == "Ivo Marina"
    assert msgs["a1"]["chat_name"] == "Ivo Marina"
    assert msgs["j1"]["sender_name"] == "Jane Doe"
    assert "sender_name" not in msgs["a2"]  # from me

    assert whatsapp.get_sender_name("222") == "Treasurer"


# --- MCP surface -------------------------------------------------------------


@pytest.fixture
def main_all(monkeypatch):
    monkeypatch.setenv("WHATSAPP_MCP_TOOLSETS", "all")
    monkeypatch.delenv("WHATSAPP_MCP_TOOLS", raising=False)
    import main

    return importlib.reload(main)


def test_tool_annotations(main_all):
    listing = main_all.mcp._tool_manager.get_tool("list_unnamed_contacts").annotations
    assert listing.read_only_hint is True
    assert listing.open_world_hint is False

    setter = main_all.mcp._tool_manager.get_tool("set_contact_names").annotations
    assert setter.read_only_hint is False
    assert setter.destructive_hint is False
    assert setter.idempotent_hint is True
    assert setter.open_world_hint is False


def test_set_contact_names_schema_names_both_fields(main_all):
    params = main_all.mcp._tool_manager.get_tool("set_contact_names").parameters
    item = params["properties"]["names"]["items"]
    if "$ref" in item:
        item = params["$defs"][item["$ref"].rsplit("/", 1)[-1]]
    assert set(item["required"]) == {"jid", "name"}


def test_tools_round_trip(main_all, naming_store):
    listed = main_all.list_unnamed_contacts()
    assert listed["contacts"][0]["jid"] == "222@lid"
    saved = main_all.set_contact_names([{"jid": "222@lid", "name": "Treasurer"}], confirmed_by_user=True)
    assert saved["saved"] == 1
    assert all(c["jid"] != "222@lid" for c in main_all.list_unnamed_contacts()["contacts"])


# --- provenance: agent-inferred names stay unverified --------------------------


def _sources(db: str) -> dict[str, str | None]:
    with sqlite3.connect(db) as conn:
        return dict(conn.execute("SELECT jid, source FROM contact_nicknames").fetchall())


def test_source_column_is_added_idempotently(naming_store):
    with sqlite3.connect(naming_store) as conn:
        assert naming.ensure_nickname_source_column(conn) is True
        assert naming.ensure_nickname_source_column(conn) is True
        cols = [r[1] for r in conn.execute("PRAGMA table_info(contact_nicknames)")]
    assert cols.count("source") == 1


def test_readers_tolerate_a_store_without_the_source_column(naming_store):
    # Legacy row written by manage_nickname before the column existed: user-set.
    with sqlite3.connect(naming_store) as conn:
        conn.execute("INSERT INTO contact_nicknames (jid, nickname) VALUES ('385111@s.whatsapp.net', 'Ivo')")
    found = identity.get_identity("385111")
    assert found is not None
    assert (found.display_name, found.name_source, found.nickname_source) == ("Ivo", "user", "user")
    assert whatsapp.list_contact_nicknames()[0]["source"] == "user"
    by_jid, _ = _contacts()
    assert "385111@s.whatsapp.net" not in by_jid


def test_unconfirmed_names_are_stored_and_shown_as_unverified(naming_store):
    result = naming.set_contact_names([{"jid": "222@lid", "name": "Your Bank"}], confirmed_by_user=False)
    assert result["results"][0]["source"] == "agent_inferred"
    assert _sources(naming_store) == {"222@lid": "agent_inferred"}

    found = identity.get_identity("222@lid")
    assert found is not None
    assert found.display_name == "Your Bank (unverified)"
    assert found.name_source == "agent_inferred"
    as_dict = found.to_dict()
    assert as_dict["name_source"] == "agent_inferred"
    assert as_dict["names"]["nickname_source"] == "agent_inferred"

    msgs = {m["id"]: m for m in whatsapp.list_messages(limit=50)}
    assert msgs["b1"]["sender_name"] == "Your Bank (unverified)"
    assert msgs["b1"]["sender_name_source"] == "agent_inferred"
    assert whatsapp.get_sender_name("222") == "Your Bank (unverified)"

    # Still unverified, so still on the list for the user to confirm.
    by_jid, _ = _contacts()
    assert by_jid["222@lid"]["unverified_name"] == "Your Bank"


def test_unconfirmed_name_never_overwrites_a_user_name(naming_store):
    naming.set_contact_names([{"jid": "385111@s.whatsapp.net", "name": "Ivo"}], confirmed_by_user=True)
    result = naming.set_contact_names([{"jid": "111@lid", "name": "Mama"}], confirmed_by_user=False)
    assert result["success"] is False
    assert "user already named" in result["results"][0]["error"]
    assert _nicknames(naming_store) == {"385111@s.whatsapp.net": "Ivo"}

    # manage_nickname is user-driven too.
    whatsapp.set_contact_nickname("385333@s.whatsapp.net", "Neighbour")
    assert _sources(naming_store)["385333@s.whatsapp.net"] == "user"
    refused = naming.set_contact_names([{"jid": "385333", "name": "Bank"}], confirmed_by_user=False)
    assert refused["failed"] == 1

    # The user confirming does replace it.
    ok = naming.set_contact_names([{"jid": "111@lid", "name": "Ivo M"}], confirmed_by_user=True)
    assert ok["results"][0]["previous"] == "Ivo"


def test_unconfirmed_name_never_replaces_an_address_book_name(naming_store):
    result = naming.set_contact_names([{"jid": "999@lid", "name": "Mama"}], confirmed_by_user=False)
    assert "address-book" in result["results"][0]["error"]
    assert _nicknames(naming_store) == {}

    # Even if one got in (written before the address book had the name), the
    # address-book name is what shows.
    with sqlite3.connect(naming_store) as conn:
        naming.ensure_nickname_source_column(conn)
        conn.execute(
            "INSERT INTO contact_nicknames (jid, nickname, source)"
            " VALUES ('441234@s.whatsapp.net', 'Mama', 'agent_inferred')"
        )
    jane = identity.get_identity("441234")
    assert jane is not None
    assert (jane.display_name, jane.name_source) == ("Jane Doe", "address_book")
    chats = {c["jid"]: c["name"] for c in whatsapp.list_chats(limit=50)}
    assert chats["441234@s.whatsapp.net"] == "Jane Doe"


def test_confirmed_by_user_must_be_a_bool(naming_store):
    result = naming.set_contact_names([{"jid": "222@lid", "name": "X"}], confirmed_by_user="yes")  # type: ignore[arg-type]
    assert result["success"] is False
    assert _nicknames(naming_store) == {}


def test_guidance_rejects_self_claims(naming_store):
    _, result = _contacts()
    guidance = result["next_step"].lower()
    assert "not evidence" in guidance and "social-engineering" in guidance
    assert "confirmed_by_user=true" in guidance


def test_confirmed_by_user_is_a_required_tool_parameter(main_all):
    params = main_all.mcp._tool_manager.get_tool("set_contact_names").parameters
    assert "confirmed_by_user" in params["required"]
    assert params["properties"]["confirmed_by_user"]["type"] == "boolean"


def _seed(db: str, *rows: tuple[str, str, str | None]) -> None:
    with sqlite3.connect(db) as conn:
        naming.ensure_nickname_source_column(conn)
        conn.executemany(
            "INSERT INTO contact_nicknames (jid, nickname, source, updated_at) VALUES (?, ?, ?, ?)",
            [(jid, nick, src, f"2026-10-2{i} 00:00:00") for i, (jid, nick, src) in enumerate(rows)],
        )


@pytest.mark.parametrize("legacy_source", [None, "", "user"])
def test_agent_write_refused_by_user_row_under_the_other_jid_form(naming_store, legacy_source):
    # The user's name is filed under the LID; the agent writes to the phone JID.
    _seed(naming_store, ("111@lid", "Ivo", legacy_source))
    result = naming.set_contact_names([{"jid": "385111@s.whatsapp.net", "name": "Bank"}], confirmed_by_user=False)
    assert result["failed"] == 1
    assert "user already named" in result["results"][0]["error"]
    assert _nicknames(naming_store) == {"111@lid": "Ivo"}


def test_agent_write_refused_even_when_an_agent_row_ranks_first_by_jid_form(naming_store):
    # The canonical-JID agent row would win a jid-form-only ordering; the user
    # row under the LID must still block the write and survive it.
    _seed(naming_store, ("111@lid", "Ivo", "user"), ("385111@s.whatsapp.net", "Guess", "agent_inferred"))
    result = naming.set_contact_names([{"jid": "385111@s.whatsapp.net", "name": "Bank"}], confirmed_by_user=False)
    assert result["failed"] == 1
    assert _nicknames(naming_store) == {"111@lid": "Ivo", "385111@s.whatsapp.net": "Guess"}


def test_agent_write_cleans_up_only_agent_rows(naming_store):
    _seed(naming_store, ("111@lid", "Old guess", "agent_inferred"))
    result = naming.set_contact_names([{"jid": "385111@s.whatsapp.net", "name": "New guess"}], confirmed_by_user=False)
    assert result["saved"] == 1
    assert _nicknames(naming_store) == {"385111@s.whatsapp.net": "New guess"}


def test_user_row_outranks_agent_row_in_display(naming_store):
    _seed(naming_store, ("111@lid", "Ivo", "user"), ("385111@s.whatsapp.net", "Bank", "agent_inferred"))
    found = identity.get_identity("385111")
    assert found is not None
    assert (found.display_name, found.name_source, found.nickname_source) == ("Ivo", "user", "user")
    msgs = {m["id"]: m for m in whatsapp.list_messages(limit=50)}
    assert (msgs["a1"]["sender_name"], msgs["a1"]["sender_name_source"]) == ("Ivo", "user")
