"""Regression tests for the source_documents.kind schema migration.

Covers the two states a pre-existing transactions.db can be in when the
widened kind CHECK ships:

1. pristine old DB: every table still references ``source_documents`` and the
   CHECK lacks the *_pdf kinds -> full rename-rebuild path.
2. crashed/semi-migrated DB (matches the production incident): the rebuild
   already happened but ``ALTER TABLE ... RENAME`` rewrote the FKs in
   import_batches/extracted_txns to ``source_documents__old`` and the old
   table was already dropped. The repair must re-point those FKs so inserts
   work again with FK enforcement ON.
"""
import os
import sqlite3
import tempfile
import unittest

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys_path not in os.sys.path:
    os.sys.path.insert(0, sys_path)

import schema  # noqa: E402

OLD_KIND = "('gmail','gopay_pdf','manual')"
NEW_KIND = "('gmail','gopay_pdf','bca_pdf','bni_pdf','manual')"


def _old_ddl():
    """Full old-era DDL where source_documents has the narrow CHECK."""
    return schema._DDL.replace(NEW_KIND, OLD_KIND)


def _seed(conn):
    conn.execute("INSERT INTO accounts(type, provider, alias) "
                 "VALUES ('ewallet','gopay','GoPay')")
    conn.execute("INSERT INTO source_documents(kind, source_key, fingerprint) "
                 "VALUES ('gopay_pdf','src-1','fp-1')")
    conn.execute("INSERT INTO import_batches(source_document_id, state) "
                 "VALUES (1, 'committed')")


class MigrationTestCase(unittest.TestCase):
    def _conn(self):
        path = os.path.join(tempfile.mkdtemp(), 't.db')
        if os.path.exists(path):
            os.remove(path)
        return schema.connect(path)

    def _fk_targets(self, conn, table):
        return sorted(r[2] for r in conn.execute(
            f'PRAGMA foreign_key_list("{table}")').fetchall())

    def test_pristine_old_db_migrates_and_exercises_fks(self):
        conn = self._conn()
        conn.executescript(_old_ddl())
        _seed(conn)

        schema.ensure_tables(conn)

        sd = conn.execute("SELECT sql FROM sqlite_master WHERE name="
                          "'source_documents'").fetchone()[0]
        self.assertIn('bca_pdf', sd)
        self.assertEqual(['source_documents'], self._fk_targets(conn, 'import_batches'))
        self.assertIn('source_documents', self._fk_targets(conn, 'extracted_txns'))
        self.assertEqual(1, conn.execute(
            'SELECT COUNT(*) FROM source_documents').fetchone()[0])
        # FK enforcement is live after migration: a dangling ref would raise
        conn.execute("INSERT INTO source_documents(kind, source_key, fingerprint) "
                     "VALUES ('bca_pdf','src-2','fp-2')")
        conn.execute("INSERT INTO import_batches(source_document_id, state) "
                     "VALUES (2, 'preview')")
        conn.execute("INSERT INTO extracted_txns(doc_id, batch_id, direction, "
                     "total_amount, occurred_at) "
                     "VALUES (2, 2, 'in', 1000, '2026-08-01T08:00:00')")
        conn.commit()

    def test_crashed_semi_migrated_db_is_repaired(self):
        """Replicates the production incident: source_documents rebuilt with the
        new kind, __old dropped, but dependent FKs still reference __old."""
        conn = self._conn()
        conn.executescript(_old_ddl())
        _seed(conn)
        # commit so the PRAGMA foreign_keys=OFF below is not silently ignored
        conn.commit()
        # simulate the half-healed state
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('ALTER TABLE source_documents RENAME TO source_documents__old')
        conn.executescript(schema._SOURCE_DOC_CREATE)
        conn.execute('INSERT INTO source_documents SELECT * FROM source_documents__old')
        conn.execute('DROP TABLE source_documents__old')
        # commit so the pragma below is not swallowed by an open transaction
        conn.commit()
        conn.execute('PRAGMA foreign_keys=ON')
        conn.commit()

        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO import_batches(source_document_id, state) "
                         "VALUES (1, 'preview')")

        schema.ensure_tables(conn)

        self.assertEqual(['source_documents'], self._fk_targets(conn, 'import_batches'))
        self.assertIn('source_documents', self._fk_targets(conn, 'extracted_txns'))
        self.assertNotIn('source_documents__old',
                         conn.execute('SELECT sql FROM sqlite_master WHERE name='
                                      "'import_batches'").fetchone()[0])
        # and writes succeed with FKs back ON
        conn.execute("INSERT INTO import_batches(source_document_id, state) "
                     "VALUES (1, 'preview')")
        conn.execute("INSERT INTO extracted_txns(doc_id, batch_id, direction, "
                     "total_amount, occurred_at) "
                     "VALUES (1, 1, 'out', 500, '2026-08-02T09:00:00')")
        conn.commit()
        self.assertEqual(2, conn.execute(
            'SELECT COUNT(*) FROM import_batches').fetchone()[0])
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM import_batches WHERE source_document_id=1 "
            "AND state='preview'").fetchone()[0])

    def test_fresh_db_is_untouched(self):
        conn = self._conn()
        schema.ensure_tables(conn)
        schema.ensure_tables(conn)
        self.assertIn('bca_pdf', conn.execute(
            'SELECT sql FROM sqlite_master WHERE name=?',
            ('source_documents',)).fetchone()[0])


if __name__ == '__main__':
    unittest.main()