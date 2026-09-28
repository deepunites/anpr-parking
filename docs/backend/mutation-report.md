# Mutation testing — tariff/domain и billing/domain (фаза 8 §1)

Инструмент: mutmut 3.8 (`[tool.mutmut]` в pyproject: `only_mutate` = tariff/domain, billing/domain; тесты —
`tests/tariff/test_engine.py`, `tests/tariff/test_engine_contract.py`, `tests/billing/test_domain.py`,
`tests/shared/test_money.py`; `uv run mutmut run`, отчёт — `uv run mutmut results`).

**Результат (финальный прогон gate, 2026-09-20): 881 мутант — убито 854, таймаут 4 (считаются убитыми: поведение изменилось), выжило 23 → 97.4 % ≥ 85 %** (первый прогон фазы: 884 / 854 / 5 / 25 = 97.2 %; после теста `test_pending_remote_never_becomes_first_paid_of_session` мутанты `reconciliation._first_paid_by_session`/`classify` убиты).

Первый прогон (до `test_engine_contract.py` и расширения `test_domain.py`) давал 660/884 = 74.7 %: выжившие
показывали, что тесты исполняли разбивку тарифа, snapshot, JSON-контракт правил и поля расхождений сверки, но не
проверяли их. Добавленные тесты закрепили контракт каждой строки breakdown, каждого поля snapshot/JSON, сообщений
валидации и границ (daily_max ровно на пороге, скидка 0/100 %, ночное окно, локальная дата по TZ площадки).

## Выжившие (23) и таймауты (4) — почему приняты

Все выжившие — обёртки текста сообщений (`XX…XX`) при проверках `pytest.raises(match=…)` как подстроки, либо
эквивалентные мутации: `Rounding.HALF_UP → None` (значение по умолчанию), `astimezone(tz) → astimezone(None)` (TZ
теста совпадает с системной), `cost += … → cost = …` при нулевом накопителе, `"DAILY_MAX" not in applied` (список
без дублей), `raw.get("free", False) → get("free", None)` под `bool()`, `start < end → start <= end` для вырожденного
ночного окна, `or True` в условии, которое и так истинно в проверяемом контексте; таймауты — мутации границ цикла по
дням (`<`→`<=`, `+1 день`→`or True`), уводящие расчёт в бесконечный цикл, — поведение изменилось, засчитаны убитыми.
Денежной логики среди выживших нет: суммы, округления, границы окон и статусы платежей покрыты.

- `billing.domain.payment.xǁPaymentǁconfirm_provider__mutmut_5` (survived)
    -        raise ValidationError("provider payment requires CARD/ONLINE and a provider")
    +        raise ValidationError("XXprovider payment requires CARD/ONLINE and a providerXX")
- `billing.domain.payment.x_check_refund__mutmut_3` (survived)
    -        raise ConflictError("refund is possible only for PAID payment")
    +        raise ConflictError("XXrefund is possible only for PAID paymentXX")
- `billing.domain.payment.x_check_refund__mutmut_9` (survived)
    -        raise ValidationError("refund amount must be positive")
    +        raise ValidationError("XXrefund amount must be positiveXX")
- `billing.domain.payment.x_check_refund__mutmut_20` (survived)
    -            "refund exceeds refundable amount",
    +            "XXrefund exceeds refundable amountXX",
- `billing.domain.payment.xǁCashierShiftǁclose__mutmut_3` (survived)
    -        raise ConflictError("shift is already closed")
    +        raise ConflictError("XXshift is already closedXX")
- `billing.domain.payment.xǁCashierShiftǁclose__mutmut_8` (survived)
    -        raise ValidationError("counted cash cannot be negative")
    +        raise ValidationError("XXcounted cash cannot be negativeXX")
- `tariff.domain.rules.xǁOvernightWindowǁcontains__mutmut_1` (survived)
    -    if self.start < self.end:
    +    if self.start <= self.end:
- `tariff.domain.rules.xǁTariffRulesǁ__post_init____mutmut_7` (survived)
    -        raise ValidationError("grace_minutes must be >= 0 and rounding_step_minutes >= 1")
    +        raise ValidationError("XXgrace_minutes must be >= 0 and rounding_step_minutes >= 1XX")
- `tariff.domain.rules.xǁTariffRulesǁ__post_init____mutmut_11` (survived)
    -            raise ValidationError("tariff prices must be non-negative")
    +            raise ValidationError("XXtariff prices must be non-negativeXX")
- `tariff.domain.rules.xǁTariffRulesǁ__post_init____mutmut_18` (survived)
    -        raise ValidationError("weekday_overrides keys must be 0..6")
    +        raise ValidationError("XXweekday_overrides keys must be 0..6XX")
- `tariff.domain.rules.xǁTariffRulesǁterms_for__mutmut_42` (survived)
    -        daily_max=override.daily_max if override.daily_max is not None else self.daily_max,
    +        daily_max=override.daily_max if (override.daily_max is not None) or True else self.daily_max,
- `tariff.domain.rules.x__override__mutmut_38` (survived)
    -        free=bool(raw.get("free", False)),
    +        free=bool(raw.get("free", None)),
- `tariff.domain.rules.x__override__mutmut_40` (survived)
    -        free=bool(raw.get("free", False)),
    +        free=bool(raw.get("free", )),
- `tariff.domain.engine.xǁAccessTermsǁ__post_init____mutmut_7` (survived)
    -        raise ValidationError("discount_percent must be within 0..100")
    +        raise ValidationError("XXdiscount_percent must be within 0..100XX")
- `tariff.domain.engine.x_calculate__mutmut_6` (survived)
    -        raise ValidationError("exit must be after entry")
    +        raise ValidationError("XXexit must be after entryXX")
- `tariff.domain.engine.x_calculate__mutmut_123` (survived)
    -    entry_day.cost += Fraction(first_amount.minor)
    +    entry_day.cost = Fraction(first_amount.minor)
- `tariff.domain.engine.x_calculate__mutmut_149` (timeout)
    -    while cursor < billing_end:
    +    while cursor <= billing_end:
- `tariff.domain.engine.x_calculate__mutmut_153` (survived)
    -        local = cursor.astimezone(tz)
    +        local = cursor.astimezone(None)
- `tariff.domain.engine.x_calculate__mutmut_205` (survived)
    -                Money.from_fraction(cost, Rounding.HALF_UP),
    +                Money.from_fraction(cost, None),
- `tariff.domain.engine.x_calculate__mutmut_224` (survived)
    -        day_amount = Money.from_fraction(acc.cost, Rounding.HALF_UP)
    +        day_amount = Money.from_fraction(acc.cost, None)
- `tariff.domain.engine.x_calculate__mutmut_252` (survived)
    -            if "DAILY_MAX" not in applied:
    +            if "XXDAILY_MAXXX" not in applied:
- `tariff.domain.engine.x_calculate__mutmut_253` (survived)
    -            if "DAILY_MAX" not in applied:
    +            if "daily_max" not in applied:
- `tariff.domain.engine.x_calculate__mutmut_265` (survived)
    -        discount = gross.percent(access.discount_percent, Rounding.HALF_UP)
    +        discount = gross.percent(access.discount_percent, None)
- `tariff.domain.engine.x__next_boundary__mutmut_21` (timeout)
    -            candidates.append(same_day if same_day > local else same_day + timedelta(days=1))
    +            candidates.append(same_day if (same_day > local) or True else same_day + timedelta(days=1))
- `tariff.domain.engine.x__next_boundary__mutmut_22` (timeout)
    -            candidates.append(same_day if same_day > local else same_day + timedelta(days=1))
    +            candidates.append(same_day if same_day >= local else same_day + timedelta(days=1))
- `tariff.domain.engine.x__next_boundary__mutmut_23` (timeout)
    -            candidates.append(same_day if same_day > local else same_day + timedelta(days=1))
    +            candidates.append(same_day if same_day > local else same_day - timedelta(days=1))
- `tariff.domain.engine.x__next_boundary__mutmut_25` (survived)
    -            candidates.append(same_day if same_day > local else same_day + timedelta(days=1))
    +            candidates.append(same_day if same_day > local else same_day + timedelta(days=2))
