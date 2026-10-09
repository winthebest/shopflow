{{ latest_state(ref('stg_bronze__customers'), ['id', 'email', 'name', 'created_at', 'updated_at']) }}
