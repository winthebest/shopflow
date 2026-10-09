{{ latest_state(
    ref('stg_bronze__order_items'),
    ['id', 'order_id', 'product_id', 'quantity', 'unit_price', 'created_at', 'updated_at']
) }}
