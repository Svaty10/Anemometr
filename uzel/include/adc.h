/**
 * @file adc.h
 * @brief Oversampled ADC0 driver (tinyAVR 2-series): battery and chip temperature.
 */

#ifndef ADC_H_
#define ADC_H_

#include "config.h"

/** Configure ADC0 (kept disabled between measurements). */
void adc_init(void);

/**
 * @brief Measure battery voltage (switches the divider on for the measurement only).
 * @param[out] vbat_mv Battery voltage in mV.
 * @return false on conversion timeout.
 */
bool adc_measure_battery(uint16_t *vbat_mv);

/**
 * @brief Chip temperature from the internal sensor (factory calibration).
 * @param[out] temp_c Temperature in degC.
 * @return false on conversion timeout or an implausible value.
 */
bool adc_measure_temp_c(int8_t *temp_c);

#endif /* ADC_H_ */
