{{ latest_state(ref('stg_bronze__products'), ['id', 'sku', 'name', 'price', 'created_at', 'updated_at']) }}
