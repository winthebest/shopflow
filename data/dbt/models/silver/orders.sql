{{ latest_state(ref('stg_bronze__orders'), ['id', 'customer_id', 'status', 'total', 'created_at', 'updated_at']) }}
