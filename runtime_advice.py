"""Explain time-budget conflicts without changing user settings or calling services."""
import math

def recommendations(config, group):
    runtime=config.get('runtime_groups',{}).get(str(group),config.get('runtime',{}))
    scheduler={"queue_timeout":25,"request_timeout":30,**config.get('model_control',{})}
    def number(source,key,default):
        value=source.get(key,default)
        return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else default
    long_wait=number(runtime,'collect_max',45)
    queue=number(scheduler,'queue_timeout',25)
    request=number(scheduler,'request_timeout',30)
    pause=number(runtime,'delay_max',12)
    ttl=number(runtime,'reply_ttl',90)
    budget=long_wait+queue+request+pause+5
    advice=[];suggested={}
    if ttl<budget:
        target=min(180,max(120,int(math.ceil(budget/10)*10)))
        if target>ttl:suggested['reply_ttl']=target
        advice.append({'code':'TimeBudgetShort','message':'长段收集、模型排队和生成可能耗尽回复有效期。',
                       'detail':f'当前有效期 {int(ttl)} 秒；按各阶段上限预留约 {int(budget)} 秒。',
                       'action':'可适当延长本群回复有效期；这只是等待上限，通常会更早完成。' if budget<=180 else '应缩短排队或请求等待；不要无限延长旧话题回复的有效期。'})
    if number(runtime,'mention_probability',.9)>=.95:
        advice.append({'code':'MentionAlreadyHigh','message':'被 @ 接话概率已经较高。',
                       'detail':'进一步提高概率不能解决断连、模型错误或回复作废。','action':'先查看回复链路与模型实测结果。'})
    if number(runtime,'cooldown_seconds',120)<=10:
        advice.append({'code':'CooldownAlreadyShort','message':'普通接话冷却已经很短。',
                       'detail':'仍会等待连续发言结束，并受每小时额度限制。','action':'无需继续缩短冷却；优先减少漏答和无效生成。'})
    return {'time_budget_seconds':int(budget),'suggested_runtime':suggested,'advice':advice}
