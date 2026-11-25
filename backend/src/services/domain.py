import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite


TIME_FORMAT = '%Y-%m-%d %H:%M'
PENDING = '待完工'
DONE = '已完工'
DEFAULT_SHIFTS = '90,120,150'


@dataclass(frozen=True)
class Order:
    code: str
    reach: str
    start: datetime
    snapshot: datetime
    load: float
    band: str
    actual: float
    recorded: datetime | None

    @property
    def day(self):
        return self.start.date().toordinal()


def sample_rows() -> list[dict]:
    first = datetime(2026, 11, 10, 8)
    patterns = [
        ('柳湾上段', 1.4, 54), ('柳湾上段', 4.6, 106),
        ('青浦桥段', 2.2, 65), ('青浦桥段', 8.5, 155),
    ]
    rows = []
    for offset in range(14):
        day = first + timedelta(days=offset)
        snapshot = (day - timedelta(days=1)).replace(hour=17, minute=30)
        for slot, (reach, load, typical) in enumerate(patterns):
            start = day + timedelta(minutes=30 * slot)
            actual = typical + ((offset * 7 + slot * 3) % 15 - 7) * 2
            recorded = (day + timedelta(days=(offset == 8 and slot == 3))).replace(hour=14)
            rows.append({
                'order_id': f'QP-{day:%m%d}-{slot + 1:02}', 'reach': reach,
                'planned_start': start.strftime(TIME_FORMAT), 'snapshot_at': snapshot.strftime(TIME_FORMAT),
                'debris_m3': load, 'status': DONE, 'duration_min': actual,
                'recorded_at': recorded.strftime(TIME_FORMAT),
            })
    for slot, (reach, load, start) in enumerate([
        ('柳湾上段', 1.8, '08:00'), ('柳湾上段', 4.9, '08:45'), ('青浦桥段', 8.1, '09:30'),
    ], start=1):
        rows.append({
            'order_id': f'QP-1124-{slot:02}', 'reach': reach,
            'planned_start': f'2026-11-24 {start}', 'snapshot_at': '2026-11-23 17:30',
            'debris_m3': load, 'status': PENDING, 'duration_min': 0, 'recorded_at': PENDING,
        })
    return rows


def _time(value, label):
    try:
        parsed = datetime.strptime(value, TIME_FORMAT)
    except (ValueError, TypeError) as error:
        raise ValueError(f'{label}请填写北京时间 YYYY-MM-DD HH:MM。') from error
    if parsed.isoformat(' ', timespec='minutes') != value or parsed.year < 1900:
        raise ValueError(f'{label}请填写 1900 年起的北京时间 YYYY-MM-DD HH:MM。')
    return parsed


def _number(value, low, high, label):
    if type(value) not in (int, float) or not low <= value <= high or not isfinite(value):
        raise ValueError(f'{label}请填写 {low} 至 {high} 之间的有限数值并核对单位。')
    return value


def _orders(rows):
    seen = set()
    orders = []
    for line, row in enumerate(rows, 2):
        try:
            code, reach = row['order_id'], row['reach']
            if not isinstance(code, str) or not code.strip() or not isinstance(reach, str) or not reach.strip():
                raise ValueError('请补填工单号与河段名称。')
            code, reach = code.strip(), reach.strip()
            if code in seen:
                raise ValueError(f'工单 {code} 重复，请保留一份用于预报的冻结快照。')
            start = _time(row['planned_start'], '计划开工')
            snapshot = _time(row['snapshot_at'], '巡查快照')
            if snapshot > start:
                raise ValueError('巡查快照晚于计划开工，请换成开工前已留存的快照。')
            load = _number(row['debris_m3'], 0, 1000, '预估漂浮物量（m³）')
            band = '低量' if load < 3 else '中量' if load < 7 else '高量'
            if row['status'] == DONE:
                actual = _number(row['duration_min'], 1, 1440, '实耗（分钟）')
                recorded = _time(row['recorded_at'], '完工台账入账')
                if recorded < start:
                    raise ValueError('完工台账入账早于计划开工，请核对开工与入账时间。')
            elif row['status'] == PENDING:
                actual = _number(row['duration_min'], 0, 0, '待完工工单实耗（分钟）')
                if row['recorded_at'] != PENDING:
                    raise ValueError('待完工工单的入账时间请填写“待完工”。')
                recorded = None
            else:
                raise ValueError('工单状态请选择“已完工”或“待完工”。')
            orders.append(Order(code, reach, start, snapshot, load, band, actual, recorded))
            seen.add(code)
        except KeyError as error:
            raise ValueError(f'第 {line} 行缺少 {error.args[0]}，请按演示 CSV 补齐字段。') from error
        except ValueError as error:
            raise ValueError(f'第 {line} 行 {error}') from error
    return sorted(orders, key=lambda order: (order.start, order.code))


def _integer(options, key, label, default, low, high):
    value = options.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}请填写 {low} 至 {high} 之间的整数。')
    return value


def _shifts(value):
    tokens = re.split('[,，]', value) if isinstance(value, str) else []
    if 2 <= len(tokens) <= 8 and all(re.fullmatch(r'[0-9]{1,3}', token.strip()) for token in tokens):
        candidates = sorted(int(token) for token in tokens)
        if len(set(candidates)) == len(candidates) and 15 <= candidates[0] <= candidates[-1] <= 720:
            return candidates
    raise ValueError('候选班次请填 2 至 8 个互不重复的 15 至 720 分钟整数，用逗号分隔。')


def _quantile(ordered, percent):
    rank = (len(ordered) * percent + 99) // 100
    return ordered[rank - 1]


def _stats(pairs, percent):
    loss = 0.0
    covered = 0
    for actual, predicted in pairs:
        error = actual - predicted
        loss += max(percent * error, (percent - 100) * error) / 100
        covered += actual <= predicted
    return round(loss / len(pairs), 4), round(covered * 100 / len(pairs), 2)


def _fit(order, cutoff, history, support):
    eligible = [item for item in history if item.recorded <= cutoff]
    global_values = sorted(item.actual for item in eligible)
    if len(global_values) < support:
        return [], global_values, '历史不足'
    same_band = [item for item in eligible if item.band == order.band]
    reach_values = sorted(item.actual for item in same_band if item.reach == order.reach)
    if len(reach_values) >= support:
        return reach_values, global_values, f'{order.reach} · {order.band}'
    if len(same_band) >= support:
        return sorted(item.actual for item in same_band), global_values, f'跨河段 · {order.band}'
    return global_values, global_values, '回退至无条件'


def _detail(kind, day='', **fields):
    record = {
        '记录': kind, '作业日': day, '工单': '', '河段': '', '预报截点': '', '巡查快照': '',
        '预估量（m³）': None, '条件组': '',
        '条件样本': None, '基线样本': None, '工单数': None, '实耗（分钟）': None,
        '条件P50（分钟）': None, '基线P50（分钟）': None,
        '条件目标分位（分钟）': None, '基线目标分位（分钟）': None,
        '条件Pinball（分钟）': None, '基线Pinball（分钟）': None,
        '条件覆盖（%）': None, '基线覆盖（%）': None, '班次（分钟）': None,
        '班次覆盖（%）': None, '处理': '',
    }
    record.update(fields)
    return record


def _summary(kind, day, scored, target):
    conditional = _stats([(actual, forecast) for actual, _, forecast, _, _ in scored], target)
    baseline = _stats([(actual, forecast) for actual, _, _, _, forecast in scored], target)
    return _detail(kind, day, **{
        '工单数': len(scored), '条件Pinball（分钟）': conditional[0], '基线Pinball（分钟）': baseline[0],
        '条件覆盖（%）': conditional[1], '基线覆盖（%）': baseline[1],
    })


def compute(rows: list[dict], options: dict) -> dict:
    window = _integer(options, 'lookback_days', '滚动历史天数', 14, 2, 90)
    support = _integer(options, 'min_support', '最少历史工单数', 4, 2, 50)
    lead = timedelta(hours=_integer(options, 'lead_hours', '提前预报小时数', 12, 1, 72))
    target = _integer(options, 'target_pct', '排班覆盖目标', 80, 50, 99)
    shifts = _shifts(options.get('candidate_shifts', DEFAULT_SHIFTS))
    orders = _orders(rows)
    if not any(order.recorded is not None for order in orders):
        raise ValueError('请导入含实耗与入账时间的已完工工单，才能建立滚动历史。')

    history = [order for order in orders if order.recorded is not None and order.snapshot <= order.start - lead]
    history_days = [order.day for order in history]
    by_day = defaultdict(list)
    backtest_rows, plan_rows = [], []
    skipped_snapshot = skipped_support = conditional_folds = 0
    planned = selected = manual = unforecasted = plan_fallbacks = 0
    for order in orders:
        cutoff = order.start - lead
        on_time = order.snapshot <= cutoff
        conditional, baseline, group = [], [], '快照迟到'
        if on_time:
            lower = bisect_left(history_days, order.day - window)
            upper = bisect_left(history_days, order.day)
            conditional, baseline, group = _fit(order, cutoff, history[lower:upper], support)
        details = {
            '工单': order.code, '河段': order.reach, '预报截点': cutoff.strftime(TIME_FORMAT),
            '巡查快照': order.snapshot.strftime(TIME_FORMAT), '预估量（m³）': order.load,
            '条件组': group, '条件样本': len(conditional), '基线样本': len(baseline),
        }
        day = date.fromordinal(order.day).isoformat()
        if not conditional:
            details['处理'] = '补录截点前的冻结快照' if not on_time else '补充截点前已入账的历史工单'
            if order.recorded is None:
                planned += 1
                unforecasted += 1
                plan_rows.append(_detail('排班预报', day, **details))
            else:
                skipped_snapshot += not on_time
                skipped_support += on_time
                backtest_rows.append(_detail('回测跳过', day, **details, **{'实耗（分钟）': order.actual}))
            continue

        cond_p50, cond_q = _quantile(conditional, 50), _quantile(conditional, target)
        base_p50, base_q = _quantile(baseline, 50), _quantile(baseline, target)
        details.update({
            '条件P50（分钟）': cond_p50, '基线P50（分钟）': base_p50,
            '条件目标分位（分钟）': cond_q, '基线目标分位（分钟）': base_q,
        })
        if order.recorded is not None:
            by_day[day].append((order.actual, cond_p50, cond_q, base_p50, base_q))
            conditional_folds += group != '回退至无条件'
            loss, coverage = _stats([(order.actual, cond_q)], target)
            baseline_loss, baseline_coverage = _stats([(order.actual, base_q)], target)
            backtest_rows.append(_detail('回测工单', day, **details, **{
                '实耗（分钟）': order.actual, '条件Pinball（分钟）': loss, '条件覆盖（%）': coverage,
                '基线Pinball（分钟）': baseline_loss, '基线覆盖（%）': baseline_coverage,
            }))
            continue

        planned += 1
        plan_fallbacks += group == '回退至无条件'
        shift = next((minutes for minutes in shifts if minutes >= cond_q), None)
        if shift is None:
            manual += 1
            decision = '安排接替班组或拆分作业，复核交接时段'
        elif group == '回退至无条件':
            manual += 1
            decision = '仅供人工排班参考，补足条件组样本后复核'
        else:
            selected += 1
            decision = f'安排 1 个班组，预留 {shift} 分钟'
        plan_rows.append(_detail('排班预报', day, **details, **{'班次（分钟）': shift, '处理': decision}))
        for minutes in shifts:
            covered = bisect_right(conditional, minutes)
            plan_rows.append(_detail('排班候选', day, **details, **{
                '班次（分钟）': minutes, '班次覆盖（%）': round(covered * 100 / len(conditional), 2),
                '处理': '达到覆盖目标' if minutes >= cond_q else '低于覆盖目标',
            }))

    scored = [item for day_scores in by_day.values() for item in day_scores]
    metrics = {
        '目标覆盖（%）': target, '可评估作业日': len(by_day), '已配对回测工单': len(scored),
        '条件组参与回测工单': conditional_folds, '快照迟到跳过工单': skipped_snapshot,
        '历史不足跳过工单': skipped_support, '待排班工单': planned, '可安排单班组工单': selected,
        '需人工排班工单': manual, '未能预报工单': unforecasted,
    }
    comparison, points = [], []
    if scored:
        for percent, cond_index, base_index in [(50, 1, 3)] + ([(target, 2, 4)] if target != 50 else []):
            for label, index in [('条件', cond_index), ('基线', base_index)]:
                loss, coverage = _stats([(item[0], item[index]) for item in scored], percent)
                metrics[f'{label} P{percent} Pinball（分钟）'] = loss
                metrics[f'{label} P{percent} 覆盖（%）'] = coverage
                points.append({'label': f'{label} P{percent}', 'value': loss})
        comparison = [_summary('回测汇总', '', scored, target)]
        comparison.extend(_summary('逐日回测', day, items, target) for day, items in by_day.items())
        for minutes in shifts:
            covered = sum(item[0] <= minutes for item in scored)
            comparison.append(_detail('班次历史覆盖', **{
                '工单数': len(scored), '班次（分钟）': minutes,
                '班次覆盖（%）': round(covered * 100 / len(scored), 2), '处理': '配对回测工单实耗覆盖率',
            }))

    blockers = []
    if len(by_day) < 2:
        blockers.append('补足至少两个可回测作业日')
    if not conditional_folds:
        blockers.append('补足同档历史以评估条件模型')
    if not planned:
        blockers.append('补充待完工工单以生成排班')
    if unforecasted:
        blockers.append('补足缺失的待排班预报输入或历史')
    if plan_fallbacks:
        blockers.append('补足回退工单的同档历史')
    notes = [
        '北京时间按工单计划开工减去提前量确定预报截点。仅用截点前或当时可见的冻结巡查，以及窗口内更早作业日且已在截点入账的完工实耗。',
        '以漂浮物预估量分档，低量小于 3 m³，中量为 3 至不足 7 m³，高量至少 7 m³。优先取同河段同档，样本不足依次取跨河段同档和无条件窗口。',
        '经验分位数取升序样本第 ceil(样本数 × 分位概率) 项。基线使用同一截点和窗口的全部合格工单，两种方法逐单配对评分。',
        'Pinball 对低估每分钟加权 q、对高估每分钟加权 1−q。覆盖率计入实耗等于分位预测的工单。整体指标按工单汇总。',
        '排班候选覆盖率由该工单的训练组估计。选达到目标的最短班次。班次历史覆盖是固定分钟数在配对回测工单上的覆盖率。',
        '班次建议按单工单的历史作业用时估计。调度交接需复核班组并发占用、航行转场和安全停工条件。',
    ]
    if blockers:
        notes.append('发布前请' + '、'.join(blockers) + '。')
    headline = f'{len(by_day)} 日滚动回测 · {selected} 单可排单班组 · {manual} 单需人工排班'
    if blockers:
        headline += ' · 待补资料'
    return {
        'headline': headline, 'metrics': metrics, 'rows': plan_rows + comparison + backtest_rows,
        'notes': notes, 'can_publish': not blockers,
        'chart': {'label': '同一批工单的分位预测 Pinball 损失', 'unit': '分钟', 'points': points},
    }
