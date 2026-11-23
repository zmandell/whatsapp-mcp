import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

import whatsapp


def build_dbs(contacts, lid_map, chats, messages=()):
    folder = tempfile.mkdtemp()
    wa = sqlite3.connect(os.path.join(folder, "whatsapp.db"))
    wa.executescript(
        """
        CREATE TABLE whatsmeow_contacts (our_jid TEXT, their_jid TEXT, first_name TEXT, full_name TEXT,
            push_name TEXT, business_name TEXT, redacted_phone TEXT);
        CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT);
        """
    )
    for jid, first, full, push, biz in contacts:
        wa.execute("INSERT INTO whatsmeow_contacts VALUES ('me', ?, ?, ?, ?, ?, '')", (jid, first, full, push, biz))
    wa.executemany("INSERT INTO whatsmeow_lid_map VALUES (?, ?)", lid_map)
    wa.commit()
    wa.close()

    msg = sqlite3.connect(os.path.join(folder, "messages.db"))
    msg.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
        CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
            is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB,
            file_enc_sha256 BLOB, file_length INTEGER, PRIMARY KEY (id, chat_jid));
        CREATE TABLE reactions (chat_jid TEXT, message_id TEXT, sender TEXT, emoji TEXT, timestamp TIMESTAMP,
            is_from_me BOOLEAN, PRIMARY KEY (chat_jid, message_id, sender));
        """
    )
    msg.executemany("INSERT INTO chats VALUES (?, ?, ?)", chats)
    msg.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, ?)",
        messages,
    )
    msg.commit()
    msg.close()
    return folder


class NameLookupTests(unittest.TestCase):
    def use(self, folder):
        whatsapp.MESSAGES_DB_PATH = os.path.join(folder, "messages.db")
        whatsapp.WHATSAPP_DB_PATH = os.path.join(folder, "whatsapp.db")
        whatsapp._name_cache.clear()

    def test_name_priority(self):
        folder = build_dbs(
            contacts=[
                ("1@s.whatsapp.net", "FirstOnly", "Full Name", "pushy", "Biz"),
                ("2@s.whatsapp.net", "OnlyFirst", "", "pushy2", ""),
                ("3@s.whatsapp.net", "", "", "Pushy Three", "Biz Three"),
                ("4@s.whatsapp.net", "", "", "", "Biz Four"),
                ("5@s.whatsapp.net", "", "", "", ""),
            ],
            lid_map=[],
            chats=[],
        )
        self.use(folder)
        self.assertEqual(whatsapp.lookup_contact_name("1"), "Full Name")
        self.assertEqual(whatsapp.lookup_contact_name("2"), "OnlyFirst")
        self.assertEqual(whatsapp.lookup_contact_name("3"), "~Pushy Three")
        self.assertEqual(whatsapp.lookup_contact_name("4"), "Biz Four")
        self.assertIsNone(whatsapp.lookup_contact_name("5"))
        self.assertIsNone(whatsapp.lookup_contact_name("999"))

    def test_lid_resolves_through_the_phone_number_map(self):
        folder = build_dbs(
            contacts=[("15550001@s.whatsapp.net", "", "Alice Smith", "", "")],
            lid_map=[("777", "15550001")],
            chats=[],
        )
        self.use(folder)
        self.assertEqual(whatsapp.lookup_contact_name("777"), "Alice Smith")
        self.assertEqual(whatsapp.lookup_contact_name("777@lid"), "Alice Smith")
        self.assertEqual(whatsapp.lookup_contact_name("15550001"), "Alice Smith")

    def test_contact_keyed_by_lid_is_found_from_the_phone_number(self):
        folder = build_dbs(
            contacts=[("888@lid", "", "Bob Jones", "", "")],
            lid_map=[("888", "15550002")],
            chats=[],
        )
        self.use(folder)
        self.assertEqual(whatsapp.lookup_contact_name("15550002"), "Bob Jones")

    def test_missing_contact_store_falls_back_quietly(self):
        folder = build_dbs(contacts=[], lid_map=[], chats=[])
        os.remove(os.path.join(folder, "whatsapp.db"))
        self.use(folder)
        self.assertIsNone(whatsapp.lookup_contact_name("1"))
        self.assertEqual(whatsapp.display_sender("1"), "1")
        self.assertEqual(whatsapp.resolve_chat_name("1@lid", "1"), "1")

    def test_chat_names(self):
        folder = build_dbs(
            contacts=[("15550001@s.whatsapp.net", "", "Alice Smith", "", "")],
            lid_map=[("777", "15550001")],
            chats=[],
        )
        self.use(folder)
        self.assertEqual(whatsapp.resolve_chat_name("777@lid", "777"), "Alice Smith")
        self.assertEqual(whatsapp.resolve_chat_name("777@lid", None), "Alice Smith")
        self.assertEqual(whatsapp.resolve_chat_name("777@lid", "Custom Label"), "Custom Label")
        self.assertEqual(whatsapp.resolve_chat_name("g1@g.us", "777"), "777")
        self.assertEqual(whatsapp.display_sender(None), None)

    def test_sender_name_never_borrows_a_group_name(self):
        folder = build_dbs(
            contacts=[],
            lid_map=[],
            chats=[("15550009-1627585436@g.us", "Some Group", None)],
        )
        self.use(folder)
        self.assertEqual(whatsapp.get_sender_name("15550009"), "15550009")

    def test_sender_name_still_falls_back_to_the_chat_name(self):
        folder = build_dbs(contacts=[], lid_map=[], chats=[("15550003@s.whatsapp.net", "Chat Named Carol", None)])
        self.use(folder)
        self.assertEqual(whatsapp.get_sender_name("15550003"), "Chat Named Carol")

    def test_phone_number_for_an_anonymized_chat(self):
        folder = build_dbs(contacts=[], lid_map=[("777", "15550001")], chats=[])
        self.use(folder)
        self.assertEqual(whatsapp.phone_for_jid("777@lid"), "15550001")
        self.assertEqual(whatsapp.phone_for_jid("15550001@s.whatsapp.net"), "15550001")
        self.assertEqual(whatsapp.phone_for_jid("404@lid"), "404")

    def test_search_finds_a_number_only_chat_by_contact_name(self):
        folder = build_dbs(
            contacts=[("15550001@s.whatsapp.net", "", "Alice Smith", "", "")],
            lid_map=[("777", "15550001")],
            chats=[("777@lid", "777", None), ("999@lid", "999", None)],
        )
        self.use(folder)
        found = whatsapp.search_contacts("alice")
        self.assertEqual([(c.jid, c.name, c.phone_number) for c in found], [("777@lid", "Alice Smith", "15550001")])
        listed = whatsapp.list_chats(query="alice")
        self.assertEqual([(c.jid, c.name) for c in listed], [("777@lid", "Alice Smith")])
        self.assertEqual(whatsapp.search_contacts("zzz-nobody"), [])

    def test_messages_and_last_sender_show_names(self):
        folder = build_dbs(
            contacts=[("15550001@s.whatsapp.net", "", "Alice Smith", "", "")],
            lid_map=[("777", "15550001")],
            chats=[("g1@g.us", "Dev Group", "2026-11-22 09:00:00")],
            messages=[("m1", "g1@g.us", "777", "ship it", "2026-11-22 09:00:00", 0)],
        )
        self.use(folder)
        message = whatsapp.Message(
            timestamp=datetime(2026, 11, 22, 9, 0, 0), sender="777", content="ship it",
            is_from_me=False, chat_jid="g1@g.us", id="m1", chat_name="Dev Group",
        )
        self.assertIn("From: Alice Smith: ship it", whatsapp.format_message(message))
        chat = whatsapp.list_chats()[0]
        self.assertEqual(chat.last_sender, "Alice Smith")
        self.assertEqual(chat.name, "Dev Group")

    def test_search_returns_saved_contacts_who_have_no_chat(self):
        folder = build_dbs(
            contacts=[
                ("15550004@s.whatsapp.net", "", "Dana Group-Only", "", ""),
                ("888@lid", "", "Erin Lid-Only", "", ""),
                ("999@lid", "", "Frank No-Map", "", ""),
                ("15550006@s.whatsapp.net", "", "", "", ""),
            ],
            lid_map=[("888", "15550005")],
            chats=[],
        )
        self.use(folder)
        found = whatsapp.search_contacts("dana")
        self.assertEqual([(c.jid, c.phone_number, c.name) for c in found],
                         [("15550004@s.whatsapp.net", "15550004", "Dana Group-Only")])
        # a LID-keyed contact comes back under the phone number send_message accepts
        found = whatsapp.search_contacts("erin")
        self.assertEqual([(c.jid, c.phone_number) for c in found], [("15550005@s.whatsapp.net", "15550005")])
        # with no phone number known, the LID itself is all there is
        found = whatsapp.search_contacts("frank")
        self.assertEqual([(c.jid, c.phone_number) for c in found], [("999@lid", "999")])
        # searching by number works, and a contact with no name at all is skipped
        self.assertEqual([c.name for c in whatsapp.search_contacts("1555000")], ["Dana Group-Only"])
        self.assertEqual(len([c for c in whatsapp.search_contacts("15550006")]), 0)

    def test_a_contact_with_a_chat_is_listed_once(self):
        folder = build_dbs(
            contacts=[
                ("15550001@s.whatsapp.net", "", "Alice Smith", "", ""),
                ("777@lid", "", "Alice Smith", "", ""),
            ],
            lid_map=[("777", "15550001")],
            chats=[("777@lid", "777", None)],
        )
        self.use(folder)
        found = whatsapp.search_contacts("alice")
        self.assertEqual([(c.jid, c.name) for c in found], [("777@lid", "Alice Smith")])

    def test_a_person_saved_under_both_forms_is_listed_once(self):
        folder = build_dbs(
            contacts=[
                ("15550007@s.whatsapp.net", "", "Gina Both", "", ""),
                ("555@lid", "", "Gina Both", "", ""),
            ],
            lid_map=[("555", "15550007")],
            chats=[],
        )
        self.use(folder)
        found = whatsapp.search_contacts("gina")
        self.assertEqual([c.jid for c in found], ["15550007@s.whatsapp.net"])

    def test_results_are_capped_at_fifty_with_chats_first(self):
        contacts = [(f"1555{i:04d}@s.whatsapp.net", "", f"Person {i:03d}", "", "") for i in range(120)]
        folder = build_dbs(contacts=contacts, lid_map=[], chats=[("1555aaaa@s.whatsapp.net", "Person Chat First", None)])
        self.use(folder)
        found = whatsapp.search_contacts("person")
        self.assertEqual(len(found), 50)
        self.assertEqual(found[0].jid, "1555aaaa@s.whatsapp.net")
        names = [c.name for c in found[1:]]
        self.assertEqual(names, sorted(names, key=str.lower))


if __name__ == "__main__":
    unittest.main()
