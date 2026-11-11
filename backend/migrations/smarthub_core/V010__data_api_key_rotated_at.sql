-- When each Data API's current key was issued. The page uses it to show how
-- old a key is and to flag ones overdue for rotation.
--
-- Existing APIs take the time of their last rotation from the audit log
-- (regenerate_key stamps "(api_id=<id>)" into details), or their creation time
-- if the key was never rotated.

ALTER TABLE data_apis ADD COLUMN key_rotated_at timestamptz;

UPDATE data_apis a
   SET key_rotated_at = COALESCE(
         (SELECT max(l."timestamp")
            FROM audit_log l
           WHERE l.action = 'data_api_key_regenerated'
             AND l.details LIKE '%(api_id=' || a.id || ')%'),
         a.created_at);

ALTER TABLE data_apis ALTER COLUMN key_rotated_at SET DEFAULT now();
ALTER TABLE data_apis ALTER COLUMN key_rotated_at SET NOT NULL;