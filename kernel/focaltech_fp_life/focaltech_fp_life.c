// SPDX-License-Identifier: GPL-2.0-only
/*
 * Lab foothold driver for FocalTech FT9362 power-button FP on Fairphone 5.
 *
 * L2 goal only: power/reset/IRQ + chardev + optional listen mode.
 * Does NOT talk QSEE / match templates. Not a full biometrics stack.
 *
 * GPIOs (TLMM, Android oracle): IRQ=34, RESET=35, VDD=60
 * Compatible: focaltech,fp (optional DT); also software platform device.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/platform_device.h>
#include <linux/of.h>
#include <linux/of_device.h>
#include <linux/clk.h>
#include <linux/gpio/consumer.h>
#include <linux/gpio/driver.h>
#include <linux/gpio/machine.h>
#include <linux/interrupt.h>
#include <linux/fs.h>
#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/uaccess.h>
#include <linux/mutex.h>
#include <linux/atomic.h>
#include <linux/delay.h>
#include <linux/slab.h>
#include <linux/ioctl.h>
#include <linux/poll.h>
#include <linux/wait.h>
#include <linux/jiffies.h>
#include <linux/pinctrl/consumer.h>
#include <linux/pinctrl/machine.h>
#include <linux/io.h>
#include <linux/interconnect.h>
#include <linux/pm.h>
#include <linux/pm_wakeup.h>

#define DRV_NAME		"focaltech_fp"
#define DEV_NAME		"focaltech_fp"
#define FF_IOC_MAGIC		'f'

/* Minimal ioctl surface (lab stubs; not Android ABI) */
#define FF_IOC_INIT_DRIVER	_IO(FF_IOC_MAGIC, 0x01)
#define FF_IOC_RESET_DEVICE	_IO(FF_IOC_MAGIC, 0x02)
#define FF_IOC_ENABLE_IRQ	_IO(FF_IOC_MAGIC, 0x03)
#define FF_IOC_DISABLE_IRQ	_IO(FF_IOC_MAGIC, 0x04)
#define FF_IOC_ENABLE_SPI_CLK	_IO(FF_IOC_MAGIC, 0x05) /* no-op: no SPI yet */
#define FF_IOC_DISABLE_SPI_CLK	_IO(FF_IOC_MAGIC, 0x06)
#define FF_IOC_ENABLE_POWER	_IO(FF_IOC_MAGIC, 0x07)
#define FF_IOC_DISABLE_POWER	_IO(FF_IOC_MAGIC, 0x08)
#define FF_IOC_GET_IRQ_COUNT	_IOR(FF_IOC_MAGIC, 0x10, unsigned int)

/* Module params for non-DT / software bind */
/*
 * Wake from suspend on a finger. GPIO 34 has PDC pin 77 on sc7280
 * (sc7280_pdc_map), so it can resume deep suspend once enable_irq_wake()
 * is set. The device is wakeup-capable; power/wakeup in sysfs turns it on
 * or off ("enabled" at probe when wake_default=1). Suspend arms the IRQ
 * as a wake source only while it is enabled (listen=1). Each edge reports
 * a wakeup event that holds the system awake wake_hold_ms, so the unlock
 * session can read the finger before the next suspend.
 */
static bool wake_default = true;
module_param(wake_default, bool, 0444);
MODULE_PARM_DESC(wake_default, "power/wakeup enabled at probe (default true)");
static unsigned int wake_hold_ms = 3000;
module_param(wake_hold_ms, uint, 0644);
MODULE_PARM_DESC(wake_hold_ms, "ms a finger edge keeps the system awake (default 3000)");

static int irq_gpio = 34;
module_param(irq_gpio, int, 0644);
MODULE_PARM_DESC(irq_gpio, "TLMM GPIO for FP IRQ (default 34)");

static int reset_gpio = 35;
module_param(reset_gpio, int, 0644);
MODULE_PARM_DESC(reset_gpio, "TLMM GPIO for FP RESET (default 35)");

static int vdd_gpio = 60;
module_param(vdd_gpio, int, 0644);
MODULE_PARM_DESC(vdd_gpio, "TLMM GPIO for FP VDD enable (default 60)");

static bool use_sw_pdev = true;
module_param(use_sw_pdev, bool, 0644);
MODULE_PARM_DESC(use_sw_pdev, "Register software platform device if no DT node (default true)");

/* TLMM pinctrl chip label on FP5 pmOS (gpiochip4 / f100000.pinctrl) */
static char *tlmm_label = "f100000.pinctrl";
module_param(tlmm_label, charp, 0644);
MODULE_PARM_DESC(tlmm_label, "gpiochip label for TLMM (default f100000.pinctrl)");

/*
 * Android: driver.spi_bus_num=14, enable_spidev=off (TA owns SPI).
 * Map: QUP wrap1 SE6 = spi@a98000, GCC_QUPV3_WRAP1_S6_CLK, pins gpio56-59 / qup16.
 * Pins are gpio-reserved-ranges on FP5 DT (for TZ); we still vote clocks from HLOS.
 */
static char *spi_se_path = "/soc@0/geniqup@ac0000/spi@a98000";
module_param(spi_se_path, charp, 0644);
MODULE_PARM_DESC(spi_se_path, "DT path of FP QUP SPI SE (default SE14 / a98000)");

static bool auto_spiclk = true;
module_param(auto_spiclk, bool, 0644);
MODULE_PARM_DESC(auto_spiclk, "Enable SPI clocks on probe/INIT (default true)");
/*
 * M4aw: wrap1 SE6 was enabled at 2.048 MHz (debugfs) while TA SPI is 4M.
 * Image path then logs configure spi mode failed / vio fail. Request 19.2M+
 * before enable (Android-ish QUP SE rate floor for 4M SPI).
 */
static ulong spi_se_rate_hz = 19200000;
module_param(spi_se_rate_hz, ulong, 0644);
MODULE_PARM_DESC(spi_se_rate_hz, "clk_set_rate for wrap1 SE6 before enable (default 19200000)");

/*
 * Bypass gpio-reserved-ranges for FP SPI pins via raw TLMM (lab only).
 * Default OFF: writing TLMM for reserved FP SPI pins has caused SoC reboots
 * on this unit; TZ is expected to own pinmux when qsee_spi_open runs.
 */
static bool force_spi_pinmux;
module_param(force_spi_pinmux, bool, 0644);
MODULE_PARM_DESC(force_spi_pinmux, "MMIO pinmux gpio56-59 to qup16 (default false, risky)");

/* TLMM base on SC7280 / FP5 */
#define FF_TLMM_BASE		0x0f100000UL
#define FF_TLMM_SIZE		0x00100000UL
/* sc7280: ctl_reg = 0x1000 * gpio, mux_bit=2, qup16 is func index 1 for 56-59 */
#define FF_TLMM_CTL(gpio)	(0x1000U * (u32)(gpio))
#define FF_TLMM_MUX_MASK	(0x7U << 2)
#define FF_TLMM_MUX_QUP16	(1U << 2)	/* funcs[1] = qup16 */
#define FF_TLMM_DRV_MASK	(0x7U << 6)
#define FF_TLMM_DRV_6MA		(0x2U << 6)	/* common SPI drive */

struct ff_life {
	struct device *dev;
	struct gpio_desc *gpiod_irq;
	struct gpio_desc *gpiod_reset;
	struct gpio_desc *gpiod_vdd;
	int irq;
	bool irq_enabled;
	bool power_on;
	bool driver_inited;
	bool spiclk_en;
	atomic_t irq_count;
	atomic_t wake_count;
	bool wake_armed;
	bool suspended;
	struct mutex lock;
	wait_queue_head_t wq;

	/* SPI SE14 clocks (TA-mediated path still needs HLOS clock vote) */
	struct clk *spi_se_clk;
	struct clk *wrap_m_ahb;
	struct clk *wrap_s_ahb;
	struct clk *wrap_core;
	struct clk *wrap_core_2x;
	/* wrap1 interconnect — was 0 while wrap0 had traffic */
	struct icc_path *icc_qup_core;
	struct icc_path *icc_qup_cfg;
	bool icc_on;
	struct pinctrl *spi_pinctrl;
	struct pinctrl_state *spi_pins_default;
	bool spi_pins_selected;
	void __iomem *tlmm;
	bool tlmm_pinmux_set;

	dev_t devt;
	struct cdev cdev;
	struct class *class;
	struct device *chardev;
	bool cdev_added;
};

static struct ff_life *g_ff;
static struct platform_device *g_sw_pdev;

static void ff_power(struct ff_life *ff, bool on)
{
	if (!ff->gpiod_vdd)
		return;
	/* ACTIVE_HIGH enable */
	gpiod_set_value_cansleep(ff->gpiod_vdd, on ? 1 : 0);
	ff->power_on = on;
	dev_info(ff->dev, "VDD %s (gpio consumer)\n", on ? "ON" : "OFF");
}

/*
 * Android vendor dmesg (successful probe):
 *   reset LOW → power ON → reset HIGH → LOW → HIGH  (leave HIGH)
 * Electrical levels with ACTIVE_HIGH gpiod: 0=low, 1=high.
 * Ending HIGH matches released/run on this board.
 */
static void ff_reset_pulse(struct ff_life *ff)
{
	if (!ff->gpiod_reset)
		return;

	gpiod_set_value_cansleep(ff->gpiod_reset, 0);	/* low */
	usleep_range(10000, 12000);
	gpiod_set_value_cansleep(ff->gpiod_reset, 1);	/* high */
	usleep_range(10000, 12000);
	gpiod_set_value_cansleep(ff->gpiod_reset, 0);	/* low */
	usleep_range(10000, 12000);
	gpiod_set_value_cansleep(ff->gpiod_reset, 1);	/* high — leave released */
	usleep_range(50000, 60000);
	dev_info(ff->dev, "RESET sequence done (left HIGH / run, Android-matched)\n");
}

/* Full re-power used before probe on Android (enable_re_power_b4_probing). */
static void ff_power_cycle(struct ff_life *ff)
{
	if (ff->gpiod_reset)
		gpiod_set_value_cansleep(ff->gpiod_reset, 0);
	ff_power(ff, false);
	usleep_range(15000, 20000);
	ff_power(ff, true);
	usleep_range(10000, 12000);
	ff_reset_pulse(ff);
}

static void ff_irq_set(struct ff_life *ff, bool enable)
{
	if (ff->irq < 0)
		return;
	if (enable && !ff->irq_enabled) {
		enable_irq(ff->irq);
		ff->irq_enabled = true;
		dev_info(ff->dev, "IRQ %d ENABLED (listen)\n", ff->irq);
	} else if (!enable && ff->irq_enabled) {
		disable_irq(ff->irq);
		ff->irq_enabled = false;
		dev_info(ff->dev, "IRQ %d DISABLED\n", ff->irq);
	}
}

/*
 * SPI clocks for Android bus 14 / QUP wrap1 SE6 (spi@a98000).
 * of_clk_get works on the DT node even when status=disabled (no geni probe).
 * Do NOT bind geni_spi — TA talks SPI; we only vote clocks + pinmux.
 */
static void ff_tlmm_pinmux_spi(struct ff_life *ff, bool enable)
{
	/*
	 * Map only the four pin control pages (0x1000 each), not the whole TLMM
	 * window — avoids large ioremap collisions with the pinctrl driver.
	 */
	static const int pins[] = { 56, 57, 58, 59 };
	unsigned int i;

	if (!force_spi_pinmux)
		return;

	for (i = 0; i < ARRAY_SIZE(pins); i++) {
		void __iomem *reg;
		u32 off = FF_TLMM_CTL(pins[i]);
		u32 val;

		reg = ioremap(FF_TLMM_BASE + off, 0x10);
		if (!reg) {
			dev_err(ff->dev, "ioremap TLMM gpio%d failed\n", pins[i]);
			continue;
		}
		val = readl(reg);
		if (enable) {
			/* mux=qup16, pull=none, drive ~6mA */
			val &= ~(FF_TLMM_MUX_MASK | 0x3U | FF_TLMM_DRV_MASK);
			val |= FF_TLMM_MUX_QUP16 | FF_TLMM_DRV_6MA;
		} else {
			val &= ~FF_TLMM_MUX_MASK;
		}
		writel(val, reg);
		dev_info(ff->dev, "TLMM gpio%d ctl = 0x%08x (%s)\n",
			 pins[i], readl(reg), enable ? "qup16" : "gpio");
		iounmap(reg);
	}
	ff->tlmm_pinmux_set = enable;
}

/* Match geni-spi defaults roughly: CORE@50MHz, CPU path 1kB/s floor */
#define FF_ICC_CORE_BW	Bps_to_icc(50000000)
#define FF_ICC_CPU_BW	Bps_to_icc(1000)

/* dt-bindings/clock/qcom,gcc-sc7280.h */
#define FF_GCC_QUPV3_WRAP1_CORE_2X_CLK	86
#define FF_GCC_QUPV3_WRAP1_CORE_CLK	87

static struct clk *ff_gcc_clk_get(unsigned int idx)
{
	struct device_node *np;
	struct of_phandle_args clkspec = {};
	struct clk *clk;

	np = of_find_node_by_path("/soc@0/clock-controller@100000");
	if (!np)
		np = of_find_compatible_node(NULL, NULL, "qcom,gcc-sc7280");
	if (!np)
		return ERR_PTR(-ENODEV);

	clkspec.np = np;
	clkspec.args_count = 1;
	clkspec.args[0] = idx;
	clk = of_clk_get_from_provider(&clkspec);
	of_node_put(np);
	return clk;
}

static void ff_icc_put(struct ff_life *ff)
{
	if (ff->icc_on) {
		if (ff->icc_qup_core)
			icc_disable(ff->icc_qup_core);
		if (ff->icc_qup_cfg)
			icc_disable(ff->icc_qup_cfg);
		ff->icc_on = false;
	}
	if (ff->icc_qup_core) {
		icc_put(ff->icc_qup_core);
		ff->icc_qup_core = NULL;
	}
	if (ff->icc_qup_cfg) {
		icc_put(ff->icc_qup_cfg);
		ff->icc_qup_cfg = NULL;
	}
}

static int ff_icc_get(struct ff_life *ff, struct device_node *np)
{
	struct device_node *old = ff->dev->of_node;
	int ret = 0;

	/*
	 * of_icc_get() needs dev->of_node pointing at the SPI SE node so it
	 * can resolve interconnects = <...> + interconnect-names.
	 */
	ff->dev->of_node = np;
	ff->icc_qup_core = of_icc_get(ff->dev, "qup-core");
	ff->icc_qup_cfg = of_icc_get(ff->dev, "qup-config");
	ff->dev->of_node = old;

	if (IS_ERR(ff->icc_qup_core)) {
		ret = PTR_ERR(ff->icc_qup_core);
		dev_warn(ff->dev, "icc qup-core: %d\n", ret);
		ff->icc_qup_core = NULL;
	}
	if (IS_ERR(ff->icc_qup_cfg)) {
		ret = PTR_ERR(ff->icc_qup_cfg);
		dev_warn(ff->dev, "icc qup-config: %d\n", ret);
		ff->icc_qup_cfg = NULL;
	}
	if (!ff->icc_qup_core && !ff->icc_qup_cfg)
		return ret ? ret : -ENODEV;
	return 0;
}

static int ff_icc_enable(struct ff_life *ff)
{
	int ret;

	if (ff->icc_on)
		return 0;
	if (ff->icc_qup_core) {
		ret = icc_set_bw(ff->icc_qup_core, FF_ICC_CORE_BW, FF_ICC_CORE_BW);
		if (ret)
			dev_warn(ff->dev, "icc_set_bw core: %d\n", ret);
		ret = icc_enable(ff->icc_qup_core);
		if (ret) {
			dev_err(ff->dev, "icc_enable core: %d\n", ret);
			return ret;
		}
	}
	if (ff->icc_qup_cfg) {
		ret = icc_set_bw(ff->icc_qup_cfg, FF_ICC_CPU_BW, FF_ICC_CPU_BW);
		if (ret)
			dev_warn(ff->dev, "icc_set_bw cfg: %d\n", ret);
		ret = icc_enable(ff->icc_qup_cfg);
		if (ret) {
			dev_err(ff->dev, "icc_enable cfg: %d\n", ret);
			if (ff->icc_qup_core)
				icc_disable(ff->icc_qup_core);
			return ret;
		}
	}
	ff->icc_on = true;
	dev_info(ff->dev, "QUP ICC enabled (core=%s cfg=%s)\n",
		 ff->icc_qup_core ? "ok" : "no",
		 ff->icc_qup_cfg ? "ok" : "no");
	return 0;
}

static void ff_spi_clk_release(struct ff_life *ff)
{
	ff_icc_put(ff);
	if (ff->spi_se_clk) {
		clk_put(ff->spi_se_clk);
		ff->spi_se_clk = NULL;
	}
	if (ff->wrap_core) {
		clk_put(ff->wrap_core);
		ff->wrap_core = NULL;
	}
	if (ff->wrap_core_2x) {
		clk_put(ff->wrap_core_2x);
		ff->wrap_core_2x = NULL;
	}
	if (ff->wrap_m_ahb) {
		clk_put(ff->wrap_m_ahb);
		ff->wrap_m_ahb = NULL;
	}
	if (ff->wrap_s_ahb) {
		clk_put(ff->wrap_s_ahb);
		ff->wrap_s_ahb = NULL;
	}
	if (ff->spi_pinctrl) {
		pinctrl_put(ff->spi_pinctrl);
		ff->spi_pinctrl = NULL;
		ff->spi_pins_default = NULL;
		ff->spi_pins_selected = false;
	}
	if (ff->tlmm_pinmux_set)
		ff_tlmm_pinmux_spi(ff, false);
	ff->tlmm = NULL;
}

static int ff_spi_clk_acquire(struct ff_life *ff)
{
	struct device_node *np, *parent;
	int ret = 0;

	if (ff->spi_se_clk)
		return 0;

	np = of_find_node_by_path(spi_se_path);
	if (!np) {
		dev_warn(ff->dev, "SPI SE node not found: %s\n", spi_se_path);
		return -ENODEV;
	}

	ff->spi_se_clk = of_clk_get(np, 0);
	if (IS_ERR(ff->spi_se_clk)) {
		ret = PTR_ERR(ff->spi_se_clk);
		dev_warn(ff->dev, "of_clk_get(se) failed: %d\n", ret);
		ff->spi_se_clk = NULL;
		of_node_put(np);
		return ret;
	}

	parent = of_get_parent(np);
	if (parent) {
		ff->wrap_m_ahb = of_clk_get(parent, 0);
		if (IS_ERR(ff->wrap_m_ahb)) {
			dev_warn(ff->dev, "wrap m-ahb clk: %ld (continuing)\n",
				 PTR_ERR(ff->wrap_m_ahb));
			ff->wrap_m_ahb = NULL;
		}
		ff->wrap_s_ahb = of_clk_get(parent, 1);
		if (IS_ERR(ff->wrap_s_ahb)) {
			dev_warn(ff->dev, "wrap s-ahb clk: %ld (continuing)\n",
				 PTR_ERR(ff->wrap_s_ahb));
			ff->wrap_s_ahb = NULL;
		}
		of_node_put(parent);
	}

	/* Interconnect for wrap1 QUP (debugfs showed qup1 at 0 before) */
	if (ff_icc_get(ff, np))
		dev_warn(ff->dev, "QUP ICC paths missing (continuing with clocks only)\n");

	/* wrap1 core clocks — were OFF even with SE s6 enabled */
	ff->wrap_core = ff_gcc_clk_get(FF_GCC_QUPV3_WRAP1_CORE_CLK);
	if (IS_ERR(ff->wrap_core)) {
		dev_warn(ff->dev, "wrap1 core clk: %ld\n", PTR_ERR(ff->wrap_core));
		ff->wrap_core = NULL;
	}
	ff->wrap_core_2x = ff_gcc_clk_get(FF_GCC_QUPV3_WRAP1_CORE_2X_CLK);
	if (IS_ERR(ff->wrap_core_2x)) {
		dev_warn(ff->dev, "wrap1 core_2x clk: %ld\n", PTR_ERR(ff->wrap_core_2x));
		ff->wrap_core_2x = NULL;
	}

	/*
	 * Pinmux: map gpio56-59 → qup16 (matches DT qup-spi14-*-state).
	 * Registered once in module_init as PIN_MAP for this driver name.
	 */
	ff->spi_pinctrl = pinctrl_get(ff->dev);
	if (IS_ERR(ff->spi_pinctrl)) {
		dev_warn(ff->dev, "pinctrl_get: %ld (clocks only)\n",
			 PTR_ERR(ff->spi_pinctrl));
		ff->spi_pinctrl = NULL;
	} else {
		ff->spi_pins_default = pinctrl_lookup_state(ff->spi_pinctrl, "spi");
		if (IS_ERR(ff->spi_pins_default)) {
			dev_warn(ff->dev, "pinctrl state 'spi': %ld\n",
				 PTR_ERR(ff->spi_pins_default));
			ff->spi_pins_default = NULL;
		}
	}

	of_node_put(np);
	dev_info(ff->dev,
		 "SPI SE14 clocks acquired (se=%s m_ahb=%s s_ahb=%s pinctrl=%s)\n",
		 ff->spi_se_clk ? "ok" : "no",
		 ff->wrap_m_ahb ? "ok" : "no",
		 ff->wrap_s_ahb ? "ok" : "no",
		 ff->spi_pins_default ? "ok" : "no");
	return 0;
}

static int ff_spi_clk_set(struct ff_life *ff, bool on)
{
	int ret;

	if (on) {
		ret = ff_spi_clk_acquire(ff);
		if (ret)
			return ret;
		if (ff->spiclk_en)
			return 0;

		if (ff->wrap_m_ahb) {
			ret = clk_prepare_enable(ff->wrap_m_ahb);
			if (ret) {
				dev_err(ff->dev, "enable m-ahb: %d\n", ret);
				return ret;
			}
		}
		if (ff->wrap_s_ahb) {
			ret = clk_prepare_enable(ff->wrap_s_ahb);
			if (ret) {
				dev_err(ff->dev, "enable s-ahb: %d\n", ret);
				if (ff->wrap_m_ahb)
					clk_disable_unprepare(ff->wrap_m_ahb);
				return ret;
			}
		}
		if (ff->wrap_core_2x) {
			/* Prefer a non-zero core_2x for GENI FIFO (was rate=0 live) */
			if (clk_set_rate(ff->wrap_core_2x, 200000000))
				(void)clk_set_rate(ff->wrap_core_2x, 100000000);
			ret = clk_prepare_enable(ff->wrap_core_2x);
			if (ret)
				dev_warn(ff->dev, "enable core_2x: %d\n", ret);
		}
		if (ff->wrap_core) {
			if (clk_set_rate(ff->wrap_core, 100000000))
				(void)clk_set_rate(ff->wrap_core, 50000000);
			ret = clk_prepare_enable(ff->wrap_core);
			if (ret)
				dev_warn(ff->dev, "enable core: %d\n", ret);
		}
		/*
		 * M4aw: raise SE rate before enable. Live smoke had SE at 2.048MHz
		 * with TA SPI @4M → configure spi mode failed during CAP/img.
		 */
		if (ff->spi_se_clk && spi_se_rate_hz) {
			unsigned long want = spi_se_rate_hz;
			unsigned long got;

			ret = clk_set_rate(ff->spi_se_clk, want);
			if (ret) {
				dev_warn(ff->dev, "clk_set_rate(se,%lu)=%d; try 32M\n",
					 want, ret);
				ret = clk_set_rate(ff->spi_se_clk, 32000000);
				if (ret)
					dev_warn(ff->dev, "clk_set_rate(se,32M)=%d\n",
						 ret);
			}
			got = clk_get_rate(ff->spi_se_clk);
			dev_info(ff->dev, "SPI SE rate request=%lu got=%lu Hz\n",
				 want, got);
		}
		ret = clk_prepare_enable(ff->spi_se_clk);
		if (ret) {
			dev_err(ff->dev, "enable se clk: %d\n", ret);
			if (ff->wrap_core)
				clk_disable_unprepare(ff->wrap_core);
			if (ff->wrap_core_2x)
				clk_disable_unprepare(ff->wrap_core_2x);
			if (ff->wrap_s_ahb)
				clk_disable_unprepare(ff->wrap_s_ahb);
			if (ff->wrap_m_ahb)
				clk_disable_unprepare(ff->wrap_m_ahb);
			return ret;
		}
		ret = ff_icc_enable(ff);
		if (ret)
			dev_warn(ff->dev, "ICC enable failed: %d (clocks still on)\n", ret);
		/* Do NOT pinctrl_select / TLMM by default — hang/reboot on this unit */
		if (force_spi_pinmux) {
			if (ff->spi_pins_default && !ff->spi_pins_selected) {
				ret = pinctrl_select_state(ff->spi_pinctrl,
							   ff->spi_pins_default);
				if (ret)
					dev_warn(ff->dev,
						 "pinctrl select spi: %d\n", ret);
				else
					ff->spi_pins_selected = true;
			}
			if (!ff->spi_pins_selected)
				ff_tlmm_pinmux_spi(ff, true);
		}
		ff->spiclk_en = true;
		dev_info(ff->dev,
			 "SPI_CLK ENABLED (wrap1 SE6 / bus 14, icc=%d se_rate=%lu)\n",
			 ff->icc_on,
			 ff->spi_se_clk ? clk_get_rate(ff->spi_se_clk) : 0UL);
	} else if (ff->spiclk_en) {
		if (ff->tlmm_pinmux_set)
			ff_tlmm_pinmux_spi(ff, false);
		if (ff->icc_on) {
			if (ff->icc_qup_core)
				icc_disable(ff->icc_qup_core);
			if (ff->icc_qup_cfg)
				icc_disable(ff->icc_qup_cfg);
			ff->icc_on = false;
		}
		if (ff->spi_se_clk)
			clk_disable_unprepare(ff->spi_se_clk);
		if (ff->wrap_core)
			clk_disable_unprepare(ff->wrap_core);
		if (ff->wrap_core_2x)
			clk_disable_unprepare(ff->wrap_core_2x);
		if (ff->wrap_s_ahb)
			clk_disable_unprepare(ff->wrap_s_ahb);
		if (ff->wrap_m_ahb)
			clk_disable_unprepare(ff->wrap_m_ahb);
		ff->spiclk_en = false;
		dev_info(ff->dev, "SPI_CLK DISABLED\n");
	}
	return 0;
}

static irqreturn_t ff_irq_handler(int irq, void *data)
{
	struct ff_life *ff = data;
	unsigned int n;
	static unsigned long last_log_jiffies;

	n = (unsigned int)atomic_inc_return(&ff->irq_count);
	if (device_may_wakeup(ff->dev)) {
		/* hard while suspending: aborts a suspend on its way in */
		pm_wakeup_dev_event(ff->dev, wake_hold_ms, READ_ONCE(ff->suspended));
		if (READ_ONCE(ff->suspended))
			atomic_inc(&ff->wake_count);
	}
	/* Rate-limit logs — dual-edge storm flooded dmesg (M4c8) */
	if (time_after(jiffies, last_log_jiffies + HZ)) {
		last_log_jiffies = jiffies;
		dev_info(ff->dev, "FP IRQ edge #%u (line=%d)\n", n, irq);
	}
	wake_up_interruptible(&ff->wq);
	return IRQ_HANDLED;
}

static int ff_open(struct inode *inode, struct file *file)
{
	struct ff_life *ff = container_of(inode->i_cdev, struct ff_life, cdev);

	file->private_data = ff;
	return 0;
}

static int ff_release(struct inode *inode, struct file *file)
{
	return 0;
}

static long ff_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct ff_life *ff = file->private_data;
	unsigned int count;
	int ret = 0;

	mutex_lock(&ff->lock);
	switch (cmd) {
	case FF_IOC_INIT_DRIVER:
		/* Match Android: re-power + reset sequence before SPI/TA */
		ff_power_cycle(ff);
		if (auto_spiclk)
			ret = ff_spi_clk_set(ff, true);
		ff->driver_inited = true;
		dev_info(ff->dev, "INIT_DRIVER (power-cycle+reset+spiclk)\n");
		break;
	case FF_IOC_RESET_DEVICE:
		ff_reset_pulse(ff);
		break;
	case FF_IOC_ENABLE_IRQ:
		ff_irq_set(ff, true);
		break;
	case FF_IOC_DISABLE_IRQ:
		ff_irq_set(ff, false);
		break;
	case FF_IOC_ENABLE_SPI_CLK:
		ret = ff_spi_clk_set(ff, true);
		break;
	case FF_IOC_DISABLE_SPI_CLK:
		ret = ff_spi_clk_set(ff, false);
		break;
	case FF_IOC_ENABLE_POWER:
		ff_power(ff, true);
		break;
	case FF_IOC_DISABLE_POWER:
		ff_irq_set(ff, false);
		ff_spi_clk_set(ff, false);
		ff_power(ff, false);
		break;
	case FF_IOC_GET_IRQ_COUNT:
		count = (unsigned int)atomic_read(&ff->irq_count);
		if (copy_to_user((void __user *)arg, &count, sizeof(count)))
			ret = -EFAULT;
		break;
	default:
		ret = -ENOTTY;
		break;
	}
	mutex_unlock(&ff->lock);
	return ret;
}

static __poll_t ff_poll(struct file *file, poll_table *wait)
{
	struct ff_life *ff = file->private_data;

	poll_wait(file, &ff->wq, wait);
	if (atomic_read(&ff->irq_count) > 0)
		return EPOLLIN | EPOLLRDNORM;
	return 0;
}

static ssize_t ff_read(struct file *file, char __user *buf, size_t len, loff_t *ppos)
{
	struct ff_life *ff = file->private_data;
	char kbuf[64];
	int n;

	if (*ppos)
		return 0;
	n = scnprintf(kbuf, sizeof(kbuf),
		      "irq_count=%u power=%d irq_en=%d inited=%d\n",
		      (unsigned int)atomic_read(&ff->irq_count),
		      ff->power_on, ff->irq_enabled, ff->driver_inited);
	if (len < (size_t)n)
		return -EINVAL;
	if (copy_to_user(buf, kbuf, n))
		return -EFAULT;
	*ppos += n;
	return n;
}

static const struct file_operations ff_fops = {
	.owner		= THIS_MODULE,
	.open		= ff_open,
	.release	= ff_release,
	.unlocked_ioctl	= ff_ioctl,
	.compat_ioctl	= ff_ioctl,
	.poll		= ff_poll,
	.read		= ff_read,
	.llseek		= noop_llseek,
};

/* sysfs */
static ssize_t irq_count_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct ff_life *ff = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%u\n", (unsigned int)atomic_read(&ff->irq_count));
}
static DEVICE_ATTR_RO(irq_count);

/* Edges that came in while the system was suspending or suspended. */
static ssize_t wake_count_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct ff_life *ff = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%u\n", (unsigned int)atomic_read(&ff->wake_count));
}
static DEVICE_ATTR_RO(wake_count);

static ssize_t listen_store(struct device *dev, struct device_attribute *attr,
			    const char *buf, size_t count)
{
	struct ff_life *ff = dev_get_drvdata(dev);
	bool on;
	int ret;

	ret = kstrtobool(buf, &on);
	if (ret)
		return ret;
	mutex_lock(&ff->lock);
	if (on) {
		/*
		 * M4cd: if already listening, only keep IRQ/clk — do NOT
		 * power-cycle (qsee re-writes listen=1 each sample and was
		 * re-sticking TA ESD / IRQ storm).
		 */
		if (!ff->irq_enabled) {
			ff_power_cycle(ff);
			if (auto_spiclk)
				ff_spi_clk_set(ff, true);
			ff->driver_inited = true;
			ff_irq_set(ff, true);
		} else {
			if (auto_spiclk)
				ff_spi_clk_set(ff, true);
			ff_irq_set(ff, true);
		}
	} else {
		ff_irq_set(ff, false);
	}
	mutex_unlock(&ff->lock);
	return count;
}

static ssize_t listen_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct ff_life *ff = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%d\n", ff->irq_enabled ? 1 : 0);
}
static DEVICE_ATTR_RW(listen);

/* Named vdd (not "power") — "power" collides with device PM sysfs. */
static ssize_t vdd_store(struct device *dev, struct device_attribute *attr,
			 const char *buf, size_t count)
{
	struct ff_life *ff = dev_get_drvdata(dev);
	bool on;
	int ret;

	ret = kstrtobool(buf, &on);
	if (ret)
		return ret;
	mutex_lock(&ff->lock);
	ff_power(ff, on);
	mutex_unlock(&ff->lock);
	return count;
}

static ssize_t vdd_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct ff_life *ff = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%d\n", ff->power_on ? 1 : 0);
}
static DEVICE_ATTR_RW(vdd);

static ssize_t spiclk_store(struct device *dev, struct device_attribute *attr,
			    const char *buf, size_t count)
{
	struct ff_life *ff = dev_get_drvdata(dev);
	bool on;
	int ret;

	ret = kstrtobool(buf, &on);
	if (ret)
		return ret;
	mutex_lock(&ff->lock);
	ret = ff_spi_clk_set(ff, on);
	mutex_unlock(&ff->lock);
	return ret ? ret : count;
}

static ssize_t spiclk_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct ff_life *ff = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%d\n", ff->spiclk_en ? 1 : 0);
}
static DEVICE_ATTR_RW(spiclk);

static struct attribute *ff_attrs[] = {
	&dev_attr_irq_count.attr,
	&dev_attr_wake_count.attr,
	&dev_attr_listen.attr,
	&dev_attr_vdd.attr,
	&dev_attr_spiclk.attr,
	NULL,
};
ATTRIBUTE_GROUPS(ff);

static int ff_get_gpios_from_dt(struct ff_life *ff, struct device *dev)
{
	ff->gpiod_irq = devm_gpiod_get_optional(dev, "irq", GPIOD_IN);
	if (IS_ERR(ff->gpiod_irq))
		return PTR_ERR(ff->gpiod_irq);

	/* Default HIGH = released/run (matches Android post-init state) */
	ff->gpiod_reset = devm_gpiod_get_optional(dev, "reset", GPIOD_OUT_HIGH);
	if (IS_ERR(ff->gpiod_reset))
		return PTR_ERR(ff->gpiod_reset);

	ff->gpiod_vdd = devm_gpiod_get_optional(dev, "vdd", GPIOD_OUT_LOW);
	if (IS_ERR(ff->gpiod_vdd))
		return PTR_ERR(ff->gpiod_vdd);

	return 0;
}

/*
 * Software path: request named GPIOs via global numbers (same offsets as
 * userspace gpiod on TLMM chip for this board). Uses gpiod_get_index on a
 * temporary fwnode-less approach via gpio_to_desc when available.
 */
/*
 * Software bind: gpiod lookup table maps TLMM chip line offsets (not global
 * linux gpio numbers) to named consumers. Registered once in module_init.
 */
static struct gpiod_lookup_table *ff_lookup;

/* Pinmux map: gpio56-59 → qup16 (FP SPI / Android bus 14) */
static struct pinctrl_map *ff_spi_maps;
static unsigned int ff_spi_maps_n;

static int ff_register_spi_pinmap(void)
{
	static const char *const pins[] = {
		"gpio56", "gpio57", "gpio58", "gpio59"
	};
	struct pinctrl_map *maps;
	unsigned int i, n = ARRAY_SIZE(pins);
	int ret;

	maps = kcalloc(n, sizeof(*maps), GFP_KERNEL);
	if (!maps)
		return -ENOMEM;

	for (i = 0; i < n; i++) {
		maps[i].dev_name = DRV_NAME;
		maps[i].name = "spi";
		maps[i].type = PIN_MAP_TYPE_MUX_GROUP;
		maps[i].ctrl_dev_name = "f100000.pinctrl";
		maps[i].data.mux.group = pins[i];
		maps[i].data.mux.function = "qup16";
	}

	ret = pinctrl_register_mappings(maps, n);
	if (ret) {
		pr_warn("%s: pinctrl_register_mappings: %d\n", DRV_NAME, ret);
		kfree(maps);
		return ret;
	}
	ff_spi_maps = maps;
	ff_spi_maps_n = n;
	pr_info("%s: SPI pinmux map registered (gpio56-59 / qup16)\n", DRV_NAME);
	return 0;
}

static void ff_unregister_spi_pinmap(void)
{
	if (ff_spi_maps) {
		pinctrl_unregister_mappings(ff_spi_maps);
		kfree(ff_spi_maps);
		ff_spi_maps = NULL;
		ff_spi_maps_n = 0;
	}
}

static int ff_register_lookup(void)
{
	struct gpiod_lookup_table *t;

	/* 3 lookups + sentinel */
	t = kzalloc(struct_size(t, table, 4), GFP_KERNEL);
	if (!t)
		return -ENOMEM;

	t->dev_id = DRV_NAME;
	t->table[0] = GPIO_LOOKUP(tlmm_label, irq_gpio, "irq", GPIO_ACTIVE_HIGH);
	t->table[1] = GPIO_LOOKUP(tlmm_label, reset_gpio, "reset", GPIO_ACTIVE_HIGH);
	t->table[2] = GPIO_LOOKUP(tlmm_label, vdd_gpio, "vdd", GPIO_ACTIVE_HIGH);
	/* table[3] zero sentinel from kzalloc */

	gpiod_add_lookup_table(t);
	ff_lookup = t;
	pr_info("%s: gpiod lookup on chip '%s' irq=%d reset=%d vdd=%d\n",
		DRV_NAME, tlmm_label, irq_gpio, reset_gpio, vdd_gpio);
	return 0;
}

static void ff_unregister_lookup(void)
{
	if (ff_lookup) {
		gpiod_remove_lookup_table(ff_lookup);
		kfree(ff_lookup);
		ff_lookup = NULL;
	}
}

static int ff_get_gpios_sw(struct ff_life *ff, struct device *dev)
{
	ff->gpiod_irq = devm_gpiod_get(dev, "irq", GPIOD_IN);
	if (IS_ERR(ff->gpiod_irq)) {
		dev_err(dev, "gpiod_get irq failed: %ld\n", PTR_ERR(ff->gpiod_irq));
		return PTR_ERR(ff->gpiod_irq);
	}

	ff->gpiod_reset = devm_gpiod_get(dev, "reset", GPIOD_OUT_HIGH);
	if (IS_ERR(ff->gpiod_reset)) {
		dev_err(dev, "gpiod_get reset failed: %ld\n", PTR_ERR(ff->gpiod_reset));
		return PTR_ERR(ff->gpiod_reset);
	}

	ff->gpiod_vdd = devm_gpiod_get(dev, "vdd", GPIOD_OUT_LOW);
	if (IS_ERR(ff->gpiod_vdd)) {
		dev_err(dev, "gpiod_get vdd failed: %ld\n", PTR_ERR(ff->gpiod_vdd));
		return PTR_ERR(ff->gpiod_vdd);
	}

	dev_info(dev, "SW gpiod claimed via lookup IRQ=%d RESET=%d VDD=%d chip=%s\n",
		 irq_gpio, reset_gpio, vdd_gpio, tlmm_label);
	return 0;
}

static int ff_setup_chardev(struct ff_life *ff)
{
	int ret;

	ret = alloc_chrdev_region(&ff->devt, 0, 1, DEV_NAME);
	if (ret)
		return ret;

	cdev_init(&ff->cdev, &ff_fops);
	ff->cdev.owner = THIS_MODULE;
	ret = cdev_add(&ff->cdev, ff->devt, 1);
	if (ret)
		goto err_unreg;
	ff->cdev_added = true;

	ff->class = class_create(DEV_NAME);
	if (IS_ERR(ff->class)) {
		ret = PTR_ERR(ff->class);
		ff->class = NULL;
		goto err_cdev;
	}
	ff->class->dev_groups = ff_groups;

	ff->chardev = device_create(ff->class, ff->dev, ff->devt, ff, DEV_NAME);
	if (IS_ERR(ff->chardev)) {
		ret = PTR_ERR(ff->chardev);
		ff->chardev = NULL;
		goto err_class;
	}
	dev_set_drvdata(ff->chardev, ff);

	dev_info(ff->dev, "chardev /dev/%s major %d minor %d\n",
		 DEV_NAME, MAJOR(ff->devt), MINOR(ff->devt));
	return 0;

err_class:
	class_destroy(ff->class);
	ff->class = NULL;
err_cdev:
	cdev_del(&ff->cdev);
	ff->cdev_added = false;
err_unreg:
	unregister_chrdev_region(ff->devt, 1);
	return ret;
}

static void ff_teardown_chardev(struct ff_life *ff)
{
	if (ff->chardev) {
		device_destroy(ff->class, ff->devt);
		ff->chardev = NULL;
	}
	if (ff->class) {
		class_destroy(ff->class);
		ff->class = NULL;
	}
	if (ff->cdev_added) {
		cdev_del(&ff->cdev);
		ff->cdev_added = false;
	}
	if (ff->devt)
		unregister_chrdev_region(ff->devt, 1);
}

static int ff_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct ff_life *ff;
	int ret;
	bool from_dt = !!dev->of_node;

	ff = devm_kzalloc(dev, sizeof(*ff), GFP_KERNEL);
	if (!ff)
		return -ENOMEM;

	ff->dev = dev;
	ff->irq = -1;
	mutex_init(&ff->lock);
	init_waitqueue_head(&ff->wq);
	atomic_set(&ff->irq_count, 0);
	platform_set_drvdata(pdev, ff);
	g_ff = ff;

	if (from_dt) {
		ret = ff_get_gpios_from_dt(ff, dev);
		if (ret) {
			dev_err(dev, "DT GPIO get failed: %d\n", ret);
			return ret;
		}
		if (!ff->gpiod_irq || !ff->gpiod_reset || !ff->gpiod_vdd) {
			dev_warn(dev, "DT incomplete; falling back to SW GPIOs\n");
			ret = ff_get_gpios_sw(ff, dev);
			if (ret)
				return ret;
		} else {
			dev_info(dev, "DT GPIOs: irq/reset/vdd acquired\n");
		}
	} else {
		ret = ff_get_gpios_sw(ff, dev);
		if (ret)
			return ret;
	}

	/* Power on + Android-matched reset (leave HIGH); vote SPI clocks */
	ff_power_cycle(ff);

	/* Vote SPI SE14 clocks early so TA INIT_SPI can open the bus */
	if (auto_spiclk) {
		ret = ff_spi_clk_set(ff, true);
		if (ret)
			dev_warn(dev, "auto SPI_CLK enable failed: %d (continuing)\n", ret);
	}

	ff->irq = gpiod_to_irq(ff->gpiod_irq);
	if (ff->irq < 0) {
		dev_err(dev, "gpiod_to_irq failed: %d\n", ff->irq);
		ff_power(ff, false);
		return ff->irq;
	}

	/*
	 * M4c9: rising-only again. Dual-edge caused IRQ storm (3000+ edges/s)
	 * flooding dmesg and drowning qsee_fp logs. Prefer rising; log rate-limited.
	 */
	ret = devm_request_threaded_irq(dev, ff->irq, NULL, ff_irq_handler,
					IRQF_TRIGGER_RISING | IRQF_ONESHOT | IRQF_NO_AUTOEN,
					DRV_NAME, ff);
	if (ret) {
		dev_err(dev, "request_irq(%d) failed: %d\n", ff->irq, ret);
		ff_power(ff, false);
		return ret;
	}
	ff->irq_enabled = false;
	device_init_wakeup(dev, wake_default);
	if (!wake_default)
		device_set_wakeup_capable(dev, true);

	ret = ff_setup_chardev(ff);
	if (ret) {
		dev_err(dev, "chardev setup failed: %d\n", ret);
		ff_power(ff, false);
		return ret;
	}

	dev_info(dev,
		 "probe OK FT9362 life: IRQ gpio=%d line=%d, RESET=%d, VDD=%d, /dev/%s (listen via sysfs or ioctl)\n",
		 irq_gpio, ff->irq, reset_gpio, vdd_gpio, DEV_NAME);
	return 0;
}

static void ff_remove(struct platform_device *pdev)
{
	struct ff_life *ff = platform_get_drvdata(pdev);

	mutex_lock(&ff->lock);
	ff_irq_set(ff, false);
	ff_spi_clk_set(ff, false);
	ff_spi_clk_release(ff);
	ff_power(ff, false);
	mutex_unlock(&ff->lock);
	ff_teardown_chardev(ff);
	device_init_wakeup(&pdev->dev, false);
	g_ff = NULL;
	dev_info(&pdev->dev, "removed\n");
}

static int ff_suspend(struct device *dev)
{
	struct ff_life *ff = dev_get_drvdata(dev);

	WRITE_ONCE(ff->suspended, true);
	ff->wake_armed = false;
	if (ff->irq >= 0 && ff->irq_enabled && device_may_wakeup(dev)) {
		if (!enable_irq_wake(ff->irq))
			ff->wake_armed = true;
	}
	dev_info(dev, "suspend: irq_en=%d wake=%d armed=%d irq_count=%u\n",
		 ff->irq_enabled, device_may_wakeup(dev), ff->wake_armed,
		 (unsigned int)atomic_read(&ff->irq_count));
	return 0;
}

static int ff_resume(struct device *dev)
{
	struct ff_life *ff = dev_get_drvdata(dev);

	if (ff->wake_armed)
		disable_irq_wake(ff->irq);
	ff->wake_armed = false;
	WRITE_ONCE(ff->suspended, false);
	dev_info(dev, "resume: irq_count=%u wake_count=%u\n",
		 (unsigned int)atomic_read(&ff->irq_count),
		 (unsigned int)atomic_read(&ff->wake_count));
	return 0;
}

static DEFINE_SIMPLE_DEV_PM_OPS(ff_pm_ops, ff_suspend, ff_resume);

static const struct of_device_id ff_of_match[] = {
	{ .compatible = "focaltech,fp" },
	{ /* sentinel */ }
};
MODULE_DEVICE_TABLE(of, ff_of_match);

static struct platform_driver ff_driver = {
	.probe = ff_probe,
	.remove = ff_remove,
	.driver = {
		.name = DRV_NAME,
		.of_match_table = ff_of_match,
		.pm = pm_sleep_ptr(&ff_pm_ops),
	},
};

static int __init ff_init(void)
{
	int ret;

	ret = ff_register_lookup();
	if (ret)
		return ret;

	/* Best-effort SPI pinmux map (clocks still work if this fails) */
	ff_register_spi_pinmap();

	ret = platform_driver_register(&ff_driver);
	if (ret) {
		ff_unregister_spi_pinmap();
		ff_unregister_lookup();
		return ret;
	}

	if (use_sw_pdev) {
		/*
		 * Software device for lab units without DT.
		 * Disable with use_sw_pdev=0 if a real OF node also binds.
		 */
		g_sw_pdev = platform_device_register_simple(DRV_NAME, -1, NULL, 0);
		if (IS_ERR(g_sw_pdev)) {
			ret = PTR_ERR(g_sw_pdev);
			g_sw_pdev = NULL;
			platform_driver_unregister(&ff_driver);
			ff_unregister_spi_pinmap();
			ff_unregister_lookup();
			return ret;
		}
		pr_info("%s: software platform device registered\n", DRV_NAME);
	}
	return 0;
}

static void __exit ff_exit(void)
{
	if (g_sw_pdev) {
		platform_device_unregister(g_sw_pdev);
		g_sw_pdev = NULL;
	}
	platform_driver_unregister(&ff_driver);
	ff_unregister_spi_pinmap();
	ff_unregister_lookup();
}

module_init(ff_init);
module_exit(ff_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("FP5 lab");
MODULE_DESCRIPTION("FocalTech FT9362 lab foothold (power/reset/IRQ/SPI-clk/chardev)");
MODULE_VERSION("0.8-wake");
