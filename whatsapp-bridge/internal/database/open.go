package database

import (
	"database/sql"
	"fmt"
)

// OpenMessageStore opens a message store on an arbitrary SQLite DSN and brings
// its schema up to date. NewMessageStore is the production path with its fixed
// store/messages.db location; this exists so other packages' tests can run
// against an in-memory database (e.g. "file:name?mode=memory&cache=shared").
func OpenMessageStore(dsn string) (*MessageStore, error) {
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("open message database: %w", err)
	}
	for _, step := range []func(*sql.DB) error{createTables, createIdentityTable, runMigrations} {
		if err := step(db); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("prepare message database: %w", err)
		}
	}
	return &MessageStore{db: db}, nil
}
