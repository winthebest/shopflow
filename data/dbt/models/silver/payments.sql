{{ latest_state(
    ref('stg_bronze__payments'),
    ['id', 'order_id', 'amount', 'status', 'provider_ref', 'created_at', 'updated_at']
) }}
