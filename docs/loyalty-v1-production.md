# Loyalty V1 Produccion

Variables requeridas:

- `LOYALTY_REWARD_TOKEN_ENCRYPTION_KEY`: secreto estable para cifrar el token publico recuperable. No rotarlo sin plan de recifrado de `loyalty_rewards.token_encrypted`.
- `LOYALTY_REWARD_PUBLIC_BASE_URL`: URL publica base usada para construir `/beneficio/<token>`, por ejemplo `https://www.kensarelectronic.com`.

Notas:

- El token publico no se guarda en claro.
- `token_hash` es la autoridad para lookup publico.
- `token_encrypted` existe solo para reimpresion y recuperacion interna del mismo QR.
- En produccion, si no existe clave de cifrado, la emision falla de forma explicita.
- `loyalty_reward_rules` define cuanto beneficio maximo se emite por la venta origen.
- `loyalty_redemption_rules` define cuanto descuento efectivo permite el total de la compra futura.
- Para loyalty, el descuento aplicado es `min(reward.reward_amount, redemption_rule.discount_amount, purchase_total)`.
- El reward sigue siendo de un solo uso: si un reward de hasta 30000 se usa por 10000, queda `redeemed` completo.
