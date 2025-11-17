import time
import yaml
import logging
import aiofiles
import voluptuous as vol
from pathlib import Path
from homeassistant.helpers import config_validation as cv, intent
from homeassistant.const import (
    Platform, ATTR_ENTITY_ID,
    CONF_ID, SERVICE_RELOAD,
    CONF_ALIAS, CONF_MODE, CONF_TRIGGER, CONF_ACTION,
)
from homeassistant.components.automation.const import (
    DOMAIN as AUTOMATION_DOMAIN,)
from homeassistant.const import (
    CONF_TRIGGERS, CONF_ACTIONS,
)

from .houzzkit import get_entities

_LOGGER = logging.getLogger(__name__)

REPEAT_EVERYDAY = "everyday"
REPEAT_WORKDAY = "weekday"
REPEAT_COUNTDOWN = "count_down"
REPEAT_OTHER = "others"

class CreateAlarmClockIntent(intent.IntentHandler):
    """Handle 创建循环闹钟 intents."""
    # Type of intent to handle
    intent_type = "AlarmClock"
    # description = "Create Alarm Clock "
    description =(
    "Create Alarm Clock 用于创建各种类型的闹钟，包括每天闹钟、工作日闹钟和倒计时闹钟。"
    "示例用法:"
    "- 创建名为'起床闹钟'的每天7:30闹钟"
    "- 创建名为'午休提醒'的1小时倒计时"
    "- 创建名为'会议提醒'的工作日14:00闹钟"
    )
    @property
    def slot_schema(self) -> dict | None:
        """返回验证 schema"""
        return {
            vol.Required("alias"): cv.string,
            vol.Required(
                "type",
                description="""
                闹钟类型:
                - everyday: 每天重复的闹钟，需要设置trigger_time
                - weekday: 工作日重复的闹钟，时间范围事，需要设置trigger_time  
                - count_down: 倒计时闹钟，需要设置hour,minute,second
                - others: 特定日期的闹钟，例如"明天早上7点"、"下周一8点"、"12月25日9点"等
                """
                ): vol.All(cv.ensure_list, [vol.In([REPEAT_EVERYDAY, REPEAT_WORKDAY, REPEAT_COUNTDOWN, REPEAT_OTHER])]),
            # vol.Required("trigger_time"): cv.string,
            vol.Optional(
                "trigger_time",
                description=(
                "触发时间，用于everyday和weekday类型的闹钟"
                "格式: 'HH:MM' 或 'HH:MM:SS'"
                "示例: '07:30' 或 '19:45:00'"
                "当type为everyday或weekday时，此字段为必填"
                )
                ): cv.string,
            vol.Optional(
                "hour",
                description = (
                "小时数，用于count_down类型的倒计时闹钟"
                "当type为count_down时，此字段为必填"
                "示例: 1 (表示1小时)"
                )    
                ): vol.All(
                vol.Coerce(int),
                vol.Range(min=0, max=23)
            ),
            vol.Optional(
                "minute",
                 description= ("分钟数，用于count_down类型的倒计时闹钟"
                 "范围: 0-59"
                 "当type为count_down时，此字段为必填"
                 "示例: 30 (表示30分钟)"
                 )                 
                ): vol.All(
                vol.Coerce(int),
                vol.Range(min=0, max=59)
            ),
            vol.Optional(
                "second", 
                description=
                (
                "秒数，用于count_down类型的倒计时闹钟"
                "当type为count_down时，此字段为必填"
                "示例: 0 (表示0秒)"
                )
                ): vol.All(
                vol.Coerce(int),
                vol.Range(min=0, max=59)
            ),
        }
    async def validate(self, slots: dict) -> bool:
        """验证槽位"""
        alarm_type_list = slots["type"]["value"]
        alarm_type = alarm_type_list[0] if isinstance(alarm_type_list, list) else alarm_type_list
        if alarm_type == REPEAT_COUNTDOWN:
            if not all(key in slots for key in ["hour", "minute", "second"]):
                _LOGGER.error(f"倒计时闹钟需要设置: hour, minute, second")
                raise intent.IntentHandleError("倒计时闹钟需要设置 hour, minute, second")
        elif alarm_type in [REPEAT_EVERYDAY, REPEAT_WORKDAY]:
            if "trigger_time" not in slots:
                _LOGGER.error(f"重复闹钟需要设置: trigger_time")
                raise intent.IntentHandleError("重复闹钟需要设置 trigger_time")
        elif alarm_type == REPEAT_OTHER:
            _LOGGER.error(f"不支持该闹钟类型，当前只支持工作日闹钟，每天闹钟，倒计时闹钟")
            raise intent.IntentHandleError(f"不支持该闹钟类型，当前只支持工作日闹钟，每天闹钟，倒计时闹钟")
        return True
    
    async def async_handle(self, intent_obj):
        """Handle the intent. """
        slots = self.async_validate_slots(intent_obj.slots)
        _LOGGER.info("slots: %s", slots)
        val = await self.validate(slots)
        if not val:
            raise intent.IntentHandleError("参数验证失败")
        alias = slots["alias"]["value"]
        alarm_type_list = slots["type"]["value"]
        alarm_type = alarm_type_list[0] if isinstance(alarm_type_list, list) else alarm_type_list
        speak_id = slots["_speaker_id"]["value"]
        triggers = []
        week_day = []
        repeat = False
        trigger_time = ""
        hours = 0
        minutes = 0
        seconds = 0
        if alarm_type == REPEAT_WORKDAY:
            trigger_time = slots["trigger_time"]["value"]
            week_day = ["mon", "tue", "wed", "thu", "fri"]
            triggers = [{
            CONF_TRIGGER: 'time',
            'at': trigger_time,
            'weekday': week_day
            }]
            repeat = True
        elif alarm_type == REPEAT_EVERYDAY:
            trigger_time = slots["trigger_time"]["value"]
            week_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]    
            triggers = [{
            CONF_TRIGGER: 'time',
            'at': trigger_time,
            'weekday': week_day
            }]
            repeat = True
        elif  alarm_type == REPEAT_COUNTDOWN:
            trigger = {
                CONF_TRIGGER: 'time_pattern',
            }
            hours = slots["hour"]["value"]
            minutes = slots["minute"]["value"]
            seconds = slots["second"]["value"]
            if hours > 0:
                trigger['hours'] = f"/{str(hours)}"
            if minutes > 0:
                trigger['minutes'] = f"/{str(minutes)}"
            if seconds > 0:
                trigger['seconds'] = f"/{str(seconds)}"
            triggers = [trigger]  
            repeat = False
              
        success, message = await create_alarm_clock_auto(intent_obj.hass, triggers, alias, speak_id, repeat)
        if success is False:
            raise intent.IntentHandleError(
                f"创建闹钟失败: {message}",
                response_key="creation_failed"
            )
        response = intent_obj.create_response()
        res = ""
        if alarm_type == REPEAT_COUNTDOWN:
            res = await self.countdown_alarm_response(alias, hours, minutes, seconds)
        elif alarm_type in [REPEAT_EVERYDAY, REPEAT_WORKDAY]:
            res = await self.repeat_alarm_response(alias, trigger_time, week_day)
            
        response.async_set_speech(res)
        return response
    
    async def repeat_alarm_response(self,alias, trigger_time, week_day):
        """格式化重复闹钟的响应语句"""
        day_names = {
            "mon": "周一",
            "tue": "周二",
            "wed": "周三",
            "thu": "周四",
            "fri": "周五",
            "sat": "周六",
            "sun": "周日"
        }
        formatted_days = ", ".join([day_names[d] for d in week_day])
        return f"已成功创建闹钟 '{alias}'，将在每周 {formatted_days} 的 {trigger_time} 触发"
    
    async def countdown_alarm_response(self,alias, hours, minutes, seconds):
        """格式化重复闹钟的响应语句"""
        return f"已成功创建闹钟 '{alias}'的 {hours}:{minutes}:{seconds} 后触发"
        

class CreateCountdownAlarmClockIntent(intent.IntentHandler):
    """Handle 倒计时闹钟."""
    # Type of intent to handle
    intent_type = "CreateCountdownAlarmClock"

    description = "Create Countdown Alarm Clock"

    @property
    def slot_schema(self):
        """返回验证 schema"""
        return {
            vol.Required("hour"): vol.All(
                vol.Coerce(int),
                vol.Range(min=0, max=23)
            ),
            vol.Required("minute"): vol.All(
                vol.Coerce(int),
                vol.Range(min=0, max=59)
            ),
            vol.Required("second"): vol.All(
                vol.Coerce(int),
                vol.Range(min=0, max=59)
            ),
            vol.Required("alias"): cv.string,
        }

    
    async def async_handle(self, intent_obj):
        """Handle the intent. """
        slots = self.async_validate_slots(intent_obj.slots)
        alias = slots["alias"]["value"]
        hours = slots["hour"]["value"]
        minutes = slots["minute"]["value"]
        seconds = slots["second"]["value"]
        speak_id = slots["_speaker_id"]["value"]
        trigger = {
            CONF_TRIGGER: 'time_pattern'
        }
        if hours > 0:
            trigger['hours'] = f"/{str(hours)}"
        if minutes > 0:
            trigger['minutes'] = f"/{str(minutes)}"
        if seconds > 0:
            trigger['seconds'] = f"/{str(seconds)}"
        triggers = [trigger]
        success, message = await create_alarm_clock_auto(intent_obj.hass, triggers, alias, speak_id, False)
        response = intent_obj.create_response()
        if success is False:
            raise intent.IntentHandleError(
                f"创建闹钟失败: {message}",
                response_key="creation_failed"
            )
        # 格式化工作日显示
        response.async_set_speech(
            f"已成功创建闹钟 '{alias}'，"
            f"的 {hours}:{minutes}:{seconds} 后触发"
        )
        return response

async def create_alarm_clock_auto(hass, triggers: list[dict], alias, speak_id, repeat: bool) -> tuple[bool, str]:
    """
        倒计时闹钟
        trigger = [{
            'trigger': 'time_pattern',
            'seconds': '/20',
        }]
        res = await create_alarm_clock_auto(hass, trigger, "闹钟1", data.get("speak_id"), False)
        循环闹钟
        week_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        trigger = [{
            CONF_TRIGGER: 'time',
            'at': trigger_time,
            'weekday': week_day
        }]
        res = await create_alarm_clock_auto(hass, trigger, "闹钟1", data.get("speak_id"), True)
    """
    config_key = str(int(time.time()))
    entries = get_entities(hass, speak_id)
    entry_id = ""
    for entry in entries:
        _LOGGER.info("speaker entry id: %s , entry name : %s", entry.entity_id, entry.name)
        if entry.name == "Alarm" or entry.name == "huan_xing":
            entry_id = entry.entity_id
    _LOGGER.info("speaker entry_id: %s",entry_id)    
    if len(entry_id) == 0:
        return False, "未找到对应的可操作设备"
    action = [{
        CONF_ACTION: 'button.press',
        'metadata': {},
        'data': {},
        'target': {
            'entity_id': entry_id
        }
    }]
    if repeat is False:
        action.append({
            CONF_ACTION: 'automation.toggle',
            'metadata': {},
            'data': {},
            'target': {
                'entity_id': '{{ this.entity_id }}'
            }
        })
    automations_config = {
        CONF_ID: f"{config_key}",
        CONF_ALIAS: alias,
        'description': '',
        CONF_TRIGGERS: triggers,
        'conditions': [],
        CONF_ACTIONS: action,
        CONF_MODE: 'single'
    }
    automations_path = Path(hass.config.path("automations.yaml"))
    try:
        # 读取现有配置（如果存在）
        if automations_path.exists():
            async with aiofiles.open(automations_path, 'r') as f:
                content = await f.read()
                existing_config = yaml.safe_load(content) or []
        else:
            existing_config = []
        #合并配置（避免重复）
        # 检查是否已经存在相同ID的自动化
        existing_ids = {automation.get(CONF_ID) for automation in existing_config}
        if automations_config.get(CONF_ID) not in existing_ids:
            existing_config.append(automations_config)

        async with aiofiles.open(automations_path, 'w') as f:
            await f.write(yaml.dump(existing_config, default_flow_style=False, sort_keys=False))
        _LOGGER.info("自动化配置已更新到 automations.yaml")
        # 5. 重新加载自动化组件
        await hass.services.async_call(
            AUTOMATION_DOMAIN, SERVICE_RELOAD, {CONF_ID: config_key}
        )
        _LOGGER.info("自动化组件已重新加载")
    except Exception as e:
        _LOGGER.error("更新自动化配置时出错: %s", str(e))
        return False, str(e)
    return True, ""
