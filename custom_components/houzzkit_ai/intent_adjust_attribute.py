import logging
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, get_args

import voluptuous as vol
from homeassistant.components import climate, cover, fan, humidifier, light
from homeassistant.const import (ATTR_ENTITY_ID, ATTR_TEMPERATURE,
                                 SERVICE_SET_COVER_POSITION, SERVICE_TURN_ON,
                                 Platform)
from homeassistant.core import State, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.util.color import RGBColor

_LOGGER = logging.getLogger(__name__)

UnsupportAdjustmentError = intent.IntentHandleError("Adjustment is not supported. Try setting it directly to the specified value.")


@dataclass
class IntentEntityState:
    name: str
    success: bool = True
    error: str | None = None
    attrs: dict[str, str | int | float] = field(default_factory=dict)

class ExtIntentResponse(intent.IntentResponse):
    def __init__(self, language: str, intent: intent.Intent | None = None) -> None:
        super().__init__(language, intent)
        self.entity_states: dict[str, IntentEntityState] = {}
        self.entity_order: list[str] = []

    def create_default_state(self, name: str):
        return IntentEntityState(name=name, attrs={})

    def set_state(self, entity: er.RegistryEntry, attrs: dict | None ={}, error: str | None = None):
        entity_id = entity.entity_id
        # Get the readable name.
        if len(entity.aliases) > 0:
            name = list(entity.aliases)[0]
        else:
            name = entity.name or ""
        state = self.entity_states.setdefault(
            entity_id, 
            self.create_default_state(name),
        )
        if attrs:
            state.attrs.update(attrs)
        if error:
            state.success = False
            state.error = f"Failed: {error}"
        
        if entity_id not in self.entity_order:
            self.entity_order.append(entity_id)
        
    @callback
    def as_dict(self) -> dict[str, Any]:
        """Return a dictionary representation of an intent response."""
        response_dict = super().as_dict()
        
        states = []
        for entity_id in self.entity_order:
            state = self.entity_states[entity_id]
            states.append(asdict(state))
        
        response_dict["states"] = states
        return response_dict

class AdjustType(Enum):
    INCREASE = 1
    SET = 0
    DECREASE = -1

DeltaSpecialValue = Literal["min", "max", "low", "medium", "high", "auto"]
DELTA_SPECIAL_VALUES: set[DeltaSpecialValue] = set(get_args(DeltaSpecialValue))

DeltaSupport = Literal["level", "number"]

@dataclass
class Delta():
    adjust: AdjustType
    value: int | float = 0
    abs_value: int | float = 0
    str_value: str = '' # 色值 FFEE00
    unit: str = ''
    special: DeltaSpecialValue | None = None
    
    def calc_target(self, current_value: float | None, level_step: float, min_change: float, min_value: float, max_value: float, supports: set[DeltaSupport]) -> int:
        """Calculate target value (level, percentage or number).
        
        Args:
            current_value: The current value.
            change_step: The supported change step.
                - For percentage e.g, 50%, 33.3%, 25%, 20%
        Returns:
            The updated target after applying the delta.
        """
        level_step = int(level_step)
        if self.special:
            if self.special == 'min':
                target_value = min_value
            elif self.special == 'max':
                target_value = max_value
            elif self.special == 'low':
                target_value = min_value
            elif self.special == 'medium':
                raise intent.IntentHandleError("unsupported")
            elif self.special == 'high':
                target_value = max_value
            return int(max(min_value, min(max_value, target_value)))
        
        if self.unit in ['level', '档']:
            # Delta is level.
            if "level" not in supports:
                raise intent.IntentHandleError(f"level adjustment is not supported")
            
            if self.adjust == AdjustType.SET:
                target_value = self.value*level_step
            else:
                if current_value is None:
                    raise UnsupportAdjustmentError
                
                # Adjust the current value to the stepped value.
                left_stepped_value = current_value//level_step*level_step
                right_stepped_value = left_stepped_value + level_step
                if current_value - left_stepped_value <= right_stepped_value - current_value:
                    stepped_current_value = left_stepped_value
                else:
                    stepped_current_value = right_stepped_value
                target_value = stepped_current_value + self.value*level_step
            return int(max(level_step, min(max_value, target_value)))
        
        # Delta is number, includes percentage.
        if "number" not in supports:
            raise intent.IntentHandleError(f"number adjustment is not supported")
        
        if self.adjust == AdjustType.SET:
            user_target_value = self.value
        else:
            if current_value is None:
                raise UnsupportAdjustmentError
            user_target_value = current_value + self.value
            
        if user_target_value == max_value:
            target_value = max_value
        else:
            # Match to the right stepped value.
            left_stepped_value = user_target_value//min_change*min_change
            right_stepped_value = left_stepped_value + min_change
            target_value = None
            # e.g., 10.5 in [10, 11]
            for valid_value in [left_stepped_value, right_stepped_value]:
                if abs(user_target_value - valid_value) < 1:
                    target_value = valid_value
                    # Align to the smaller value when descrease or set.
                    if self.adjust == AdjustType.DECREASE or self.adjust == AdjustType.SET:
                        break
            if target_value is None:
                raise intent.IntentHandleError(f"violates the change step: {min_change}")
        return int(max(min_value, min(max_value, target_value)))
        

def parse_delta(raw: str):
    """Parse raw value str to readable object."""
    if raw in DELTA_SPECIAL_VALUES:
        return Delta(
            adjust=AdjustType.SET,
            special=raw,
        )
    elif raw.startswith("#"):
        # color hex value
        raw = raw.upper()
        hex_color_pattern = r'^#([0-9A-F]{3,6})$'
        m = re.search(hex_color_pattern, raw)
        if not m:
            return
        
        color_value = m.groups()[0]
        return Delta(
            adjust=AdjustType.SET,
            str_value=color_value,
            unit='#',
        )
    else:
        m = re.search(r'^([+-]?)\s?(\d+\.\d+|\d+)\s?(.*)$', raw)
        if not m:
            return
        mark, value_raw, unit = m.groups()
        
        if value_raw.find('.') != -1:
            abs_value = float(value_raw)
        else:
            abs_value = int(value_raw)
        value = abs_value
        
        if mark == '+':
            adjust = AdjustType.INCREASE
        elif mark == '-':
            adjust = AdjustType.DECREASE
            value = value * -1
        else:
            adjust = AdjustType.SET
            
        return Delta(
            adjust=adjust,
            value=value,
            abs_value=abs_value,
            unit=unit.lower(),
        )

@dataclass
class AdjustmentContext:
    state: State
    delta: Delta

@dataclass
class AdjustmentTarget:
    service: str = ""
    service_data: dict = field(default_factory=dict)
    attributes: dict | None = None
    
adjustment_functions: dict[str, dict[str, Callable[[AdjustmentContext, AdjustmentTarget], None]]] = {}

supported_domain_list = set()
supported_attribute_list = set()

def register_adjustment(domain: str, attrbute: str):
    def decorator(func):
        supported_domain_list.add(domain)
        supported_attribute_list.add(attrbute)
        attrbute_handlers = adjustment_functions.setdefault(domain, {})
        attrbute_handlers[attrbute] = func
        
        def wrapper(state: State, service_data: dict[str, Any], attributes: dict, delta: Delta):
            func(state, service_data, attributes, delta)
        return wrapper
    return decorator


@register_adjustment("light", "brightness")
def adjust_light_brightness(ctx: AdjustmentContext, target: AdjustmentTarget):
    percentage_step = 10
    target.attributes = {
        "max_level": int(100//percentage_step),
        "adjustment_step": f"{percentage_step}%",
    }
    
    current_percent = None
    if ctx.delta.adjust != AdjustType.SET:
        current_brightness = ctx.state.attributes.get(light.ATTR_BRIGHTNESS)
        if current_brightness is None:
            raise UnsupportAdjustmentError
        current_percent = round(current_brightness/254*100)
    
    target_percent = ctx.delta.calc_target(current_percent, percentage_step, 1, 1, 100, supports={"number", "level"})
    target.service_data[light.ATTR_BRIGHTNESS_PCT] = target_percent
    
    target.attributes["updated_brightness"] = f"{target_percent}%"
    target.service = SERVICE_TURN_ON


@register_adjustment("light", "color")
def adjust_light_color(ctx: AdjustmentContext, target: AdjustmentTarget):
    target.attributes = {}
    
    hex_color = ctx.delta.str_value
    if len(hex_color) == 3:
        hex_color = ''.join([c * 2 for c in hex_color])
    
    # 十六进制转十进制
    r = int(hex_color[0:2], 16)  
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    target_color = RGBColor(r, g, b)
        
    target.service = SERVICE_TURN_ON
    target.service_data[light.ATTR_RGB_COLOR] = target_color
    target.attributes["updated_value"] = f"#{hex_color}"


@register_adjustment("light", "temperature")
def adjust_light_temperature(ctx: AdjustmentContext, target: AdjustmentTarget):
    
    color_temperature_min = ctx.state.attributes.get(light.ATTR_MIN_COLOR_TEMP_KELVIN, 2000)
    color_temperature_max = ctx.state.attributes.get(light.ATTR_MAX_COLOR_TEMP_KELVIN, 6500)
    color_temperature_step = 500
    
    if ctx.delta.unit == "%":
        # Convert percentage to Kelvin
        ctx.delta.value = int(ctx.delta.value/100 * (color_temperature_max - color_temperature_min))
        if ctx.delta.adjust == AdjustType.SET:
            ctx.delta.value += color_temperature_min
        ctx.delta.unit = "K"
    
    target.attributes = {
        "min_value": f"{color_temperature_min}K", 
        "max_value": f"{color_temperature_max}K",
        "adjustment_step": f"{color_temperature_step}K",
    }
    
    current_color_temperature = None
    if ctx.delta.adjust != AdjustType.SET:
        current_color_temperature: float | None = ctx.state.attributes.get(light.ATTR_COLOR_TEMP_KELVIN)
        if current_color_temperature is None:
            raise UnsupportAdjustmentError
        
    target_temperature = ctx.delta.calc_target(current_color_temperature, color_temperature_step, 1, color_temperature_min, color_temperature_max, supports={"number", "level"})
    target.service = SERVICE_TURN_ON
    target.service_data[light.ATTR_COLOR_TEMP_KELVIN] = target_temperature
    target.attributes["updated_value"] = f"{target_temperature}K"


@register_adjustment("fan", "fan_speed")
def adjust_fan_speed(ctx: AdjustmentContext, target: AdjustmentTarget):
    percentage_step = ctx.state.attributes.get(fan.ATTR_PERCENTAGE_STEP, 25)
    target.attributes = {
        "max_level": 100//int(percentage_step),
        "adjustment_step": f"{int(percentage_step)}%",
    }
    current_percent = None
    # Percentage or Level
    if ctx.delta.unit != "%":
        ctx.delta.unit = "level"
    if ctx.delta.adjust != AdjustType.SET:
        current_percent = ctx.state.attributes.get(fan.ATTR_PERCENTAGE)
        if current_percent is None:
            raise UnsupportAdjustmentError

    target_percent = ctx.delta.calc_target(current_percent, percentage_step, percentage_step, percentage_step, 100, supports={"number", "level"})
    # Fix 33%*3 case.
    if target_percent >= 99:
        target_percent = 100
    target.service = SERVICE_TURN_ON
    target.service_data[fan.ATTR_PERCENTAGE] = target_percent
    target.attributes["updated_level"] = int(target_percent//int(percentage_step))


@register_adjustment("climate", "fan_speed")
def adjust_climate_fan_speed(ctx: AdjustmentContext, target: AdjustmentTarget):
    fan_modes: list[str] = ctx.state.attributes.get(climate.const.ATTR_FAN_MODES, [])
    if len(fan_modes) == 0:
        raise intent.IntentHandleError("unsupported")
    
    target.attributes = {
        "fan_modes": fan_modes,
    }
    
    if ctx.delta.special and ctx.delta.special in ["auto", "low", "medium", "high"]:
        target_fan_mode = ctx.delta.special
        if target_fan_mode in fan_modes:
            target.service = climate.const.SERVICE_SET_FAN_MODE
            target.service_data[climate.const.ATTR_FAN_MODE] = target_fan_mode
            target.attributes["fan_mode"] = target_fan_mode
            return
        raise intent.IntentHandleError("unsupported the mode")
    
    # 档位排除掉自动
    if fan_modes[0] == "auto":
        fan_modes = fan_modes[1:]
    
    percentage_step = 100//len(fan_modes)
    target.attributes = {
        "max_level": len(fan_modes),
        "adjustment_step": f"{int(percentage_step)}%",
    }
    
    # Percentage or Level
    current_percent = None
    if ctx.delta.unit != "%":
        ctx.delta.unit = "level"
    if ctx.delta.adjust != AdjustType.SET:
        current_mode = ctx.state.attributes.get(climate.const.ATTR_FAN_MODE)
        if current_mode is None or current_mode == "auto":
            raise UnsupportAdjustmentError
        
        mode_index = fan_modes.index(current_mode)
        # 33% 66% 99%
        current_percent = (mode_index+1)*100//len(fan_modes)

    target_percent = ctx.delta.calc_target(current_percent, percentage_step, percentage_step, percentage_step, 100, supports={"number", "level"})
    # Set fan mode.
    if target_percent >= 99:
        target_percent = 100
    
    # 50*25/100
    _LOGGER.info(f"adjust_climate_fan_speed: current_percent={current_percent} target_percent={target_percent}")
    target_mode_index = min(target_percent//percentage_step-1, len(fan_modes) - 1)
    target_fan_mode = fan_modes[target_mode_index]
    target.service = climate.const.SERVICE_SET_FAN_MODE
    target.service_data[climate.const.ATTR_FAN_MODE] = target_fan_mode
    target.attributes["updated_level"] = target_mode_index
    target.attributes["fan_mode"] = target_fan_mode


@register_adjustment("climate", "temperature")
def adjust_climate_temperature(ctx: AdjustmentContext, target: AdjustmentTarget):
    if ctx.delta.unit == "%":
        raise intent.IntentHandleError("unsupported percentage")
    
    if ctx.delta.unit in ["档", "level"]:
        ctx.delta.unit = "度"
        
    min_temperature = ctx.state.attributes.get(climate.const.ATTR_MIN_TEMP, 10)
    max_temperature = ctx.state.attributes.get(climate.const.ATTR_MAX_TEMP, 30)
    temperature_step = ctx.state.attributes.get(climate.const.ATTR_TARGET_TEMP_STEP, 1)
    temperature_step = max(temperature_step, 1) # >=1
    target.attributes = {
        "adjustment_step": temperature_step,
        "min_value": min_temperature,
        "max_value": max_temperature,
        "hvac_mode": ctx.state.state,
    }
    
    current_temperature: float | None = None
    if ctx.delta.adjust != AdjustType.SET:
        current_temperature: float | None = ctx.state.attributes.get("temperature")
        if current_temperature is None:
            raise UnsupportAdjustmentError
        
    target_temperature = ctx.delta.calc_target(current_temperature, temperature_step, 1, min_temperature, max_temperature, supports={"number"})
    target.service = climate.const.SERVICE_SET_TEMPERATURE
    target.service_data[ATTR_TEMPERATURE] = target_temperature
    target.attributes["updated_value"] = target_temperature


@register_adjustment("humidifier", "humidity")
def adjust_humidifier_humidity(ctx: AdjustmentContext, target: AdjustmentTarget):
    min_value = ctx.state.attributes.get(humidifier.const.ATTR_MIN_HUMIDITY, 0)
    max_value = ctx.state.attributes.get(humidifier.const.ATTR_MAX_HUMIDITY, 100)
    adjustment_step = 10
    target.attributes = {
        "adjustment_step": f"{adjustment_step}%",
        "min_value": f"{min_value}%",
        "max_value": f"{max_value}%",
    }
    
    current_value: float | None = None
    if ctx.delta.adjust != AdjustType.SET:
        current_value: float | None = ctx.state.attributes.get(humidifier.const.ATTR_HUMIDITY)
        if current_value is None or (current_value < min_value):
            raise UnsupportAdjustmentError
        
    target_value = ctx.delta.calc_target(current_value, adjustment_step, 1, min_value, max_value, supports={"number", "level"})
    target.service = humidifier.const.SERVICE_SET_HUMIDITY
    target.service_data[humidifier.const.ATTR_HUMIDITY] = target_value
    target.attributes["updated_value"] = f"{target_value}%"
    

@register_adjustment("cover", "position")
def adjust_cover_position(ctx: AdjustmentContext, target: AdjustmentTarget):
    percentage_step = 10
    target.attributes = {
        "adjustment_step": f"{percentage_step}%",
    }
    current_percent = None
    if ctx.delta.adjust != AdjustType.SET:
        current_percent = ctx.state.attributes.get(cover.ATTR_CURRENT_POSITION)
        if current_percent is None:
            raise UnsupportAdjustmentError
    
    target_percent = ctx.delta.calc_target(current_percent, percentage_step, 1, 0, 100, supports={"number"})
    target.service = SERVICE_SET_COVER_POSITION
    target.service_data[cover.ATTR_POSITION] = target_percent
    target.attributes["updated_value"] = f"{target_percent}%"
    
    
@register_adjustment("media_player", "volume")
def adjust_media_player_volume(ctx: AdjustmentContext, target: AdjustmentTarget):
    raise intent.IntentHandleError("unsupport")


@register_adjustment("media_player", "brightness")
def adjust_media_player_brightness(ctx: AdjustmentContext, target: AdjustmentTarget):
    raise intent.IntentHandleError("unsupport")

    
class AdjustDeviceAttributeIntent(intent.IntentHandler):
    intent_type = "AdjustDeviceAttribute"
    description = "Set or adjust the numerical value of device attribute."
    slot_schema = {
        vol.Required("domain"): vol.Any(*supported_domain_list),
        vol.Required("attribute"): vol.Any(*supported_attribute_list),
        vol.Required("delta"): intent.non_empty_string,
        vol.Optional("name"): cv.string,
        vol.Optional("area"): cv.string,
        vol.Optional("floor"): cv.string,
        vol.Optional("preferred_area_id"): cv.string,
        vol.Optional("preferred_floor_id"): cv.string,
    } # type: ignore
    platforms = {Platform.LIGHT, Platform.FAN, Platform.COVER, Platform.CLIMATE, Platform.MEDIA_PLAYER}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)

        attribute: str = slots.get("attribute", {}).get("value")
        delta_raw: str = slots.get("delta", {}).get("value")
        domain: str = slots.get("domain", {}).get("value")
        name: str | None  = slots.get("name", {}).get("value")
        area_name: str | None = slots.get("area", {}).get("value")
        floor_name: str | None = slots.get("floor", {}).get("value")
        
        _LOGGER.info(
            f"AdjustDeviceAttribute params: "
            f"attribute={attribute} delta={delta_raw} domain={domain} "
            f"name={name} area_name={area_name} floor_name={floor_name}"
        )
        
        delta = parse_delta(delta_raw)
        if not delta:
            raise intent.IntentHandleError(
                f"invalid value: {delta_raw}"
            )
        
        
        response = ExtIntentResponse(intent_obj.language, intent=intent_obj)
        success_results = []
        
        match_constraints = intent.MatchTargetsConstraints(
            name=name,
            area_name=area_name,
            floor_name=floor_name,
            domains={domain},
            assistant=intent_obj.assistant,
            single_target=False,
        )
        match_preferences = intent.MatchTargetsPreferences(
            area_id=slots.get("preferred_area_id", {}).get("value"),
            floor_id=slots.get("preferred_floor_id", {}).get("value"),
        )
        match_result = intent.async_match_targets(
            hass, match_constraints, match_preferences
        )
        if not match_result.is_match:
            raise intent.MatchFailedError(
                result=match_result, constraints=match_constraints
            )
        assert match_result.states
        for state in match_result.states:
            _LOGGER.info(f"AdjustDeviceAttribute state: {state.as_dict_json}")
            entity_id = state.entity_id
            entity_registry = er.async_get(hass)
            entity = entity_registry.async_get(entity_id)
            if not entity:
                continue
            
            error: str | None = None
            target = AdjustmentTarget()
            try:
                prepare_adjustment = adjustment_functions.get(domain, {}).get(attribute)
                if not prepare_adjustment:
                    raise intent.IntentHandleError("unspported")
                
                # Find the paramters to adjust.
                prepare_adjustment(AdjustmentContext(state=state, delta=delta), target)
                target.service_data[ATTR_ENTITY_ID] = state.entity_id
                
                # Perform adjustment.
                _LOGGER.info(f"AdjustDeviceAttribute call target: {asdict(target)}")
                await hass.services.async_call(
                    domain,
                    target.service,
                    service_data=target.service_data,
                    blocking=True,
                    context=intent_obj.context,
                )
            except (intent.IntentHandleError, ServiceValidationError) as e:
                error = str(e)
                
            response.set_state(entity, target.attributes, error)
            success_results.append(intent.IntentResponseTarget(
                type=intent.IntentResponseTargetType.ENTITY,
                name=state.name,
                id=state.entity_id,
            ))

        if len(success_results) > 0:
            response.response_type = intent.IntentResponseType.ACTION_DONE
            response.success_results = success_results
        else:
            response.response_type = intent.IntentResponseType.ERROR
            response.error_code = intent.IntentResponseErrorCode.NO_VALID_TARGETS
        return response
