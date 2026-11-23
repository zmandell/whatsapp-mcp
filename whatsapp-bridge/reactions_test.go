package main

import (
	"database/sql"
	"testing"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

func newReactionStore(t *testing.T) *MessageStore {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	_, err = db.Exec(`CREATE TABLE reactions (
		chat_jid TEXT, message_id TEXT, sender TEXT, emoji TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
		PRIMARY KEY (chat_jid, message_id, sender));`)
	if err != nil {
		t.Fatal(err)
	}
	return &MessageStore{db: db}
}

func emojisOn(t *testing.T, s *MessageStore, chat, msg string) map[string]string {
	t.Helper()
	rows, err := s.db.Query("SELECT sender, emoji FROM reactions WHERE chat_jid = ? AND message_id = ?", chat, msg)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	got := map[string]string{}
	for rows.Next() {
		var sender, emoji string
		if err := rows.Scan(&sender, &emoji); err != nil {
			t.Fatal(err)
		}
		got[sender] = emoji
	}
	return got
}

func TestStoreReaction(t *testing.T) {
	s := newReactionStore(t)
	now := time.Date(2026, 11, 22, 22, 0, 0, 0, time.UTC)

	for _, r := range []struct{ sender, emoji string }{{"alice", "👍"}, {"bob", "❤️"}} {
		if err := s.StoreReaction("c@g.us", "m1", r.sender, r.emoji, now, false); err != nil {
			t.Fatal(err)
		}
	}
	if got := emojisOn(t, s, "c@g.us", "m1"); len(got) != 2 || got["alice"] != "👍" || got["bob"] != "❤️" {
		t.Fatalf("two senders should each keep a reaction, got %v", got)
	}

	// the same sender reacting again replaces the earlier emoji
	if err := s.StoreReaction("c@g.us", "m1", "alice", "😂", now.Add(time.Minute), false); err != nil {
		t.Fatal(err)
	}
	if got := emojisOn(t, s, "c@g.us", "m1"); len(got) != 2 || got["alice"] != "😂" {
		t.Fatalf("a new emoji should replace the old one, got %v", got)
	}

	// an empty emoji removes that sender's reaction and leaves the others
	if err := s.StoreReaction("c@g.us", "m1", "alice", "", now.Add(2*time.Minute), false); err != nil {
		t.Fatal(err)
	}
	if got := emojisOn(t, s, "c@g.us", "m1"); len(got) != 1 || got["bob"] != "❤️" {
		t.Fatalf("an empty emoji should remove only that sender's reaction, got %v", got)
	}

	// removing something that was never stored is not an error
	if err := s.StoreReaction("c@g.us", "m1", "carol", "", now, false); err != nil {
		t.Fatalf("removing a missing reaction should be a no-op: %v", err)
	}

	// reactions are scoped to their chat and message
	if got := emojisOn(t, s, "c@g.us", "m2"); len(got) != 0 {
		t.Fatalf("reactions must not leak across messages, got %v", got)
	}
}
