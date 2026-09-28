# Покрытие по слоям (фаза 8 §1)

Генерируется `scripts/coverage_layers.py` из `coverage.json`.

| Слой | Порог | Покрытие | Строк | Статус |
|---|---|---|---|---|
| tariff/domain — деньги | ≥ 95 % | 100.0 % | 232/232 | ✅ |
| billing/domain — деньги | ≥ 95 % | 100.0 % | 263/263 | ✅ |
| parking/domain — сессии, сага | ≥ 90 % | 94.6 % | 229/242 | ✅ |
| access/domain — безопасность | ≥ 90 % | 97.2 % | 70/72 | ✅ |
| прочие */domain | ≥ 85 % | 92.9 % | 536/577 | ✅ |
| */application | ≥ 85 % | 91.9 % | 3554/3866 | ✅ |
| */infrastructure | ≥ 70 % | 87.3 % | 1494/1711 | ✅ |
| api | ≥ 80 % | 94.0 % | 1426/1517 | ✅ |
| platform/shared/runtime/workers | ≥ 70 % | 85.8 % | 1638/1908 | ✅ |
| **проект** | ≥ 80 % | **90.9 %** | | ✅ |

| Модуль/слой | Покрытие |
|---|---|
| access/application | 95.9 % (233/243) |
| access/domain | 97.2 % (70/72) |
| access/infrastructure | 94.7 % (71/75) |
| anpr/application | 88.3 % (414/469) |
| anpr/domain | 100.0 % (75/75) |
| anpr/infrastructure | 93.7 % (178/190) |
| audit/application | 100.0 % (27/27) |
| audit/infrastructure | 100.0 % (12/12) |
| billing/application | 91.8 % (991/1080) |
| billing/domain | 100.0 % (263/263) |
| billing/infrastructure | 88.4 % (496/561) |
| devices/application | 91.1 % (602/661) |
| devices/domain | 89.5 % (247/276) |
| devices/infrastructure | 80.1 % (297/371) |
| identity/application | 95.7 % (292/305) |
| identity/domain | 98.6 % (137/139) |
| identity/infrastructure | 95.1 % (117/123) |
| media/application | 74.8 % (86/115) |
| media/domain | 92.6 % (25/27) |
| media/infrastructure | 64.7 % (55/85) |
| parking/application | 94.1 % (665/707) |
| parking/domain | 94.6 % (229/242) |
| parking/infrastructure | 88.0 % (161/183) |
| tariff/application | 96.6 % (143/148) |
| tariff/domain | 100.0 % (232/232) |
| tariff/infrastructure | 95.9 % (47/49) |
| tenancy/application | 91.0 % (101/111) |
| tenancy/domain | 86.7 % (52/60) |
| tenancy/infrastructure | 96.8 % (60/62) |
