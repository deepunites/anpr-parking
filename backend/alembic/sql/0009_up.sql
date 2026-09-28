-- 0009 Права фазы 3.
-- user_lot — не журнал, а производная привязка «пользователь → площадки»; пересобирается целиком при смене скоупа.
GRANT DELETE ON user_lot TO app_critical;
-- Версия тарифа неизменяема по правилам, но её окно действия закрывается при публикации следующей версии:
-- колоночный UPDATE только на validity (I-12 держит EXCLUDE), rules/plan_id/version остаются без UPDATE.
GRANT UPDATE (validity) ON tariff_plan_version TO app_critical;
