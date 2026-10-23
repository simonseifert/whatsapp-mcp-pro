package database

import (
	"database/sql"
	"errors"
	"fmt"
	"strings"
)

// normaliseSendersSQL mirrors migrations/008_normalise_senders.sql statement for
// statement; TestNormaliseSendersSQLMatchesMigration keeps them identical. See
// the migration header for why rows are classified in this order.
var normaliseSendersSQL = []string{
	`UPDATE messages
SET sender = chat_jid
WHERE sender <> '' AND instr(sender, '@') = 0 AND sender NOT GLOB '*[^0-9]*'
  AND chat_jid NOT LIKE '%@g.us'
  AND instr(chat_jid, '@') > 0
  AND substr(chat_jid, 1, instr(chat_jid, '@') - 1) = sender`,
	`UPDATE messages
SET sender = sender || '@lid'
WHERE sender <> '' AND instr(sender, '@') = 0 AND sender NOT GLOB '*[^0-9]*'
  AND EXISTS (SELECT 1 FROM identities i WHERE i.lid = messages.sender || '@lid')`,
	`UPDATE messages
SET sender = sender || '@s.whatsapp.net'
WHERE sender <> '' AND instr(sender, '@') = 0 AND sender NOT GLOB '*[^0-9]*'
  AND EXISTS (
    SELECT 1 FROM identities i
    WHERE i.phone_jid = messages.sender || '@s.whatsapp.net'
       OR i.jid = messages.sender || '@s.whatsapp.net'
       OR i.phone = messages.sender
  )`,
}

// NormaliseBareSenders rewrites bare-digit messages.sender values written
// before the bridge stored full JIDs (migrations/008). It is idempotent and
// only touches rows it has evidence for, so it is safe on every start; rows the
// identity directory cannot classify yet stay bare and are picked up by a
// later run. Returns the number of rows rewritten.
func (store *MessageStore) NormaliseBareSenders() (int64, error) {
	return normaliseBareSenders(store.db)
}

func normaliseBareSenders(db *sql.DB) (int64, error) {
	tx, err := db.Begin()
	if err != nil {
		return 0, fmt.Errorf("begin sender normalisation: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	var total int64
	for _, stmt := range normaliseSendersSQL {
		res, err := tx.Exec(stmt)
		if err != nil {
			return 0, fmt.Errorf("normalise senders: %w", err)
		}
		if n, err := res.RowsAffected(); err == nil {
			total += n
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("commit sender normalisation: %w", err)
	}
	return total, nil
}

// IsKnownLID reports whether the identity directory has seen user as a LID.
// A bare number is ambiguous between a phone number and a LID; callers turning
// one into a JID must not guess "phone" for digits that are really a LID, or a
// message meant for one person is addressed to whoever owns that phone number.
// A missing identities table counts as "not known".
func (store *MessageStore) IsKnownLID(user string) (bool, error) {
	user = strings.TrimSpace(user)
	if user == "" {
		return false, nil
	}
	var one int
	err := store.db.QueryRow(`SELECT 1 FROM identities WHERE lid = ? LIMIT 1`, user+"@lid").Scan(&one)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		if strings.Contains(err.Error(), "no such table") {
			return false, nil
		}
		return false, fmt.Errorf("lookup lid %s: %w", user, err)
	}
	return true, nil
}
