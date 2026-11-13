"""Pre-execution estimates, deliberately separate from actual usage and limits."""
import math


def finite(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def preview(request, estimate):
    duration, cost = finite(estimate.get('time_est_ms')), finite(estimate.get('cost_est'))
    currency = str(estimate.get('cost_currency') or '').upper()
    known_price = cost is not None and currency in {'USD', 'CNY'}
    confirm = request.permission.require_confirm or bool(estimate.get('need_confirm')) or str(request.ask) == 'propose_only'
    reasons = []
    if request.budget.timeout_ms is not None and duration is not None and duration > request.budget.timeout_ms:
        confirm = True
        reasons.append('预计耗时超过本次时间上限')
    if request.budget.cost_cap is not None and cost is not None and cost > request.budget.cost_cap:
        confirm = True
        reasons.append('预计费用超过本次费用上限')
    timing = '时间暂无可靠估计' if duration is None else f'预计约{duration/1000:g}秒'
    price = f'预计模型/执行器费用约{cost:g} {currency}' if known_price else '模型/执行器费用暂无可靠金额或币种依据'
    basis = str(estimate.get('basis') or ('同工具历史统计，仅供参考' if estimate.get('observed_runs') else '工具策划估计，尚未实测'))[:240]
    text = '执行前预估：' + timing + '；' + price + '；外部服务费用未核实，不代表免费。依据：' + basis + '。预估不是承诺值，不包含尚未计入的策划及后续重试费用。'
    if reasons:
        text += '；'.join(reasons) + '。'
    text += '等待确认，尚未执行。' if confirm else '普通任务将继续执行。'
    return {'text': text, 'time_est_ms': duration, 'cost_est': cost if known_price else None,
            'currency': currency if known_price else '', 'need_confirm': confirm}
