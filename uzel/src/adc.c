/**
 * @file adc.c
 * @brief Oversampled ADC0 driver for the ATtiny1624 (tinyAVR 2-series): battery and
 *        the chip temperature.
 *
 * The 2-series ADC differs from 0/1-series and AVR-Dx parts:
 *  - reference is selected in ADC0.CTRLC (REFSEL) together with TIMEBASE,
 *  - conversions are started via ADC0.COMMAND (MODE + START),
 *  - the accumulated result is a 32-bit ADC0.RESULT.
 *
 * Each "burst" is one BURST-mode conversion of 64 accumulated 12-bit samples
 * (max 64 * 4095 = 262080). We scale bursts to a 16x-oversampled count
 * (sum / 4, max 65520) which keeps all following math in 32 bits.
 */

#include "adc.h"
#include "power.h"
#include <util/delay.h>

#define ADC_TIMEOUT_LOOPS     60000UL      /* >> 64 samples * ~20 us */
#define ADC_TIMEBASE_VAL      ((F_CPU + 999999UL) / 1000000UL)   /* CLK_PER cycles per 1 us */
#define ADC_SAMPDUR_VAL       16U           /* extra sample time for high-Z dividers */

/* Simple insertion sort for tiny arrays (n <= 16). */
static void sort_u32(uint32_t *a, uint8_t n)
{
    for (uint8_t i = 1; i < n; i++) {
        uint32_t key = a[i];
        int8_t j = (int8_t)(i - 1);
        while (j >= 0 && a[j] > key) {
            a[j + 1] = a[j];
            j--;
        }
        a[j + 1] = key;
    }
}

void adc_init(void)
{
    ADC0.CTRLA = 0;                              /* disabled until needed */
    ADC0.CTRLB = ADC_PRESC_DIV4_gc;              /* 3.333 MHz / 4 = 833 kHz */
    ADC0.CTRLE = ADC_SAMPDUR_VAL;
    ADC0.CTRLF = ADC_SAMPNUM_ACC64_gc;           /* 64 samples per burst */
    ADC0.MUXNEG = 0;
    ADC0.PGACTRL = 0;                            /* PGA off (saves current) */
}

static void adc_start(uint8_t refsel, uint8_t muxpos)
{
    ADC0.CTRLC = refsel | (uint8_t)(ADC_TIMEBASE_VAL << ADC_TIMEBASE_gp);
    ADC0.MUXPOS = muxpos;
    ADC0.CTRLA = ADC_ENABLE_bm;
    _delay_us(100);                              /* internal reference start-up */
}

static void adc_stop(void)
{
    ADC0.CTRLA = 0;
}

/** One burst (64 accumulated samples) -> 16x-oversampled count (0..65520). */
static bool adc_burst_x16(uint32_t *out_x16)
{
    ADC0.INTFLAGS = ADC_RESRDY_bm;
    ADC0.COMMAND = ADC_MODE_BURST_gc | ADC_START_IMMEDIATE_gc;

    uint32_t timeout = ADC_TIMEOUT_LOOPS;
    while (!(ADC0.INTFLAGS & ADC_RESRDY_bm)) {
        if (--timeout == 0) {
            ADC0.COMMAND = 0;                    /* stop */
            return false;
        }
    }
    uint32_t sum = ADC0.RESULT;                  /* reading RESULT clears RESRDY */
    *out_x16 = sum >> 2;                         /* 64 samples -> 16x scale */
    return true;
}

bool adc_measure_battery(uint16_t *vbat_mv)
{
    uint32_t b[4];
    bool ok = true;

    PORTA.OUTSET = PIN_EN_BAT_MEASURE;           /* divider on */
    power_sleep_ms(BAT_SETTLE_MS);               /* MCU sleeps while the divider settles */
    adc_start(ADC_REFSEL_1024MV_gc, ADC_MUXPOS_AIN5_gc);

    uint32_t dummy;
    (void)adc_burst_x16(&dummy);                 /* flush reference transient */
    for (uint8_t i = 0; i < 4 && ok; i++) {
        ok = adc_burst_x16(&b[i]);
    }

    adc_stop();
    PORTA.OUTCLR = PIN_EN_BAT_MEASURE;           /* divider off immediately */

    if (!ok) {
        return false;
    }

    sort_u32(b, 4);
    uint32_t x16 = (b[1] + b[2]) / 2U;           /* median of 4 */

    /* pin_uV = x16 * 1024000 / 65536 = x16 * 125 / 8   (max ~1.02e6)
     * vbat_mV = pin_uV * (RTOP + RBOT) / RBOT / 1000                 */
    uint32_t pin_uv = (x16 * 125UL) / 8UL;
    uint32_t mv = (pin_uv * (BAT_RTOP_K + BAT_RBOT_K)) / (BAT_RBOT_K * 1000UL);
    *vbat_mv = (mv > 65534UL) ? 65534U : (uint16_t)mv;
    return true;
}

bool adc_measure_temp_c(int8_t *temp_c)
{
    /* Internal temperature sensor against the 1.024 V reference, datasheet
     * procedure: T[K] = ((adc10 - offset) * gain + 0x80) >> 8 with the factory
     * calibration of the signature row (TEMPSENSE0 = gain, TEMPSENSE1 = offset).
     * Accuracy ~+-3 degC – enough for the frost guard and the box temperature.
     * Long sample time: the sensor has a high output impedance. */
    adc_start(ADC_REFSEL_1024MV_gc, ADC_MUXPOS_TEMPSENSE_gc);
    ADC0.CTRLE = 0xFF;                           /* SAMPDUR max (>= 32 us at 833 kHz) */
    ADC0.CTRLF = ADC_SAMPNUM_ACC16_gc;

    uint32_t sum = 0;
    bool ok = adc_burst_x16(&sum);               /* 16 x 12 bit, sum >> 2 */
    adc_stop();
    adc_init();                                  /* back to the battery configuration */
    if (!ok) {
        return false;
    }
    /* adc_burst_x16 returned (16 samples sum) / 4 = 4 x average 12-bit value;
     * 10-bit value = average / 4 = sum16 / 64 = returned / 16 */
    int32_t adc10 = (int32_t)((sum + 8UL) / 16UL);
    int16_t offset = (int8_t)SIGROW.TEMPSENSE1;
    uint8_t gain = SIGROW.TEMPSENSE0;
    int32_t k = ((adc10 - offset) * (int32_t)gain + 0x80) >> 8;
    int32_t c = k - 273;
    if (c < -45 || c > 100) {
        return false;                            /* implausible: treat as unknown */
    }
    *temp_c = (int8_t)c;
    return true;
}
