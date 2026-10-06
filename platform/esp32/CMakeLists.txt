idf_component_register(
  SRCS "pal.c" "../common/file.c" "../../dcr/src/runtime.c"
       "../../dcr/src/lifecycle.c" "../../dcr/src/port.c" "../../dcr/src/bus.c" "../../dcr/src/safety.c"
       "../../dcr/src/config.c" "../../dcr/src/validator.c"
       "../../components/common/software.c" "../../components/sensors/environment.c"
       "../../components/sensors/i2c_bus.c" "../../components/sensors/sensor_components.c"
       "i2c_bus.c"
  INCLUDE_DIRS "../include" "../../dcr/include" "../../components/common" "../../components/sensors"
  REQUIRES json esp_timer freertos esp_driver_i2c esp_driver_uart)
if(DCR_CONTROL)
  target_sources(${COMPONENT_LIB} PRIVATE "../../components/control/control.c"
                                           "../../components/control/control_uart_esp.c")
  target_include_directories(${COMPONENT_LIB} PRIVATE "../../components/control")
endif()
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
