<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { AlertCircle, CalendarRange, CheckCircle2, TrendingDown, TrendingUp } from "@lucide/vue";
import type { AnalyticsCard, AnalyticsMetric } from "./types";

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const props = defineProps<{ card: AnalyticsCard }>();
const trendElement = ref<HTMLDivElement | null>(null);
const breakdownElement = ref<HTMLDivElement | null>(null);
let trendChart: echarts.ECharts | null = null;
let breakdownChart: echarts.ECharts | null = null;
let resizeObserver: ResizeObserver | null = null;

const amountFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const escapeHtml = (value: unknown) => String(value ?? "")
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;")
  .replace(/'/g, "&#39;");
const currencyUnits = new Set(["cny", "rmb", "人民币", "元", "currency", "¥"]);
const isCurrencyMetric = (metric: AnalyticsMetric) => (
  currencyUnits.has(metric.unit.trim().toLowerCase())
  || metric.unit.trim().toUpperCase() === props.card.currency.toUpperCase()
);
const currencySymbol = computed(() => props.card.currency === "CNY" ? "¥" : props.card.currency);
const formatValue = (value: number, metric: AnalyticsMetric) => {
  if (isCurrencyMetric(metric)) {
    return Math.abs(value) >= 10000
      ? `${currencySymbol.value} ${amountFormatter.format(value / 10000)} 万`
      : `${currencySymbol.value} ${amountFormatter.format(value)}`;
  }
  if (metric.unit === "%") return `${amountFormatter.format(value)}%`;
  return `${amountFormatter.format(value)}${metric.unit ? ` ${metric.unit}` : ""}`;
};
const metricValue = (metric: AnalyticsMetric) => {
  return formatValue(metric.value, metric);
};
const changeLabel = (metric: AnalyticsMetric) => {
  if (metric.unit === "%" && metric.change_value !== null) {
    return `${metric.change_value >= 0 ? "+" : ""}${metric.change_value.toFixed(1)}pt`;
  }
  if (metric.change_rate === null) return "无对比";
  return `${metric.change_rate >= 0 ? "+" : ""}${metric.change_rate.toFixed(1)}%`;
};
const chartSubtitle = computed(() => `统计周期 · ${props.card.period_label}`);
const primaryMetric = computed(() => (
  props.card.metrics.find((metric) => metric.key === props.card.trend_metric_key)
  ?? props.card.metrics.find(isCurrencyMetric)
  ?? props.card.metrics[0]
));
const trendValue = (point: AnalyticsCard["trend"][number]) => {
  const configuredKey = props.card.trend_metric_key || point.metric_key || primaryMetric.value?.key;
  const configuredValue = configuredKey ? point[configuredKey] : undefined;
  if (typeof configuredValue === "number") return configuredValue;
  if (typeof point.value === "number") return point.value;
  const fallback = Object.entries(point).find(([key, value]) => (
    typeof value === "number" && !["order_count"].includes(key)
  ));
  return typeof fallback?.[1] === "number" ? fallback[1] : 0;
};
const primaryMetricLabel = computed(() => primaryMetric.value?.label ?? "指标");
const primaryValueFormatter = (value: number) => primaryMetric.value
  ? formatValue(value, primaryMetric.value)
  : amountFormatter.format(value);
const breakdownChartType = computed(() => (
  props.card.breakdown_chart_type ?? (props.card.breakdown.length > 4 ? "bar" : "pie")
));
const breakdownRanking = computed(() => (
  [...props.card.breakdown]
    .sort((left, right) => right.value - left.value)
    .slice(0, 8)
));
const breakdownSubtitle = computed(() => {
  if (breakdownChartType.value !== "bar") return `${primaryMetricLabel.value}占比`;
  const visibleCount = Math.min(props.card.breakdown.length, breakdownRanking.value.length);
  return `${primaryMetricLabel.value}排名 · 前 ${visibleCount} 名`;
});

function renderCharts() {
  if (!trendElement.value || !breakdownElement.value) return;
  trendChart ??= echarts.init(trendElement.value);
  breakdownChart ??= echarts.init(breakdownElement.value);
  trendChart.setOption({
    animationDuration: 500,
    color: ["#1687f8"],
    tooltip: {
      trigger: "axis",
      valueFormatter: primaryValueFormatter,
    },
    grid: { left: 10, right: 14, top: 22, bottom: 8, containLabel: true },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: props.card.trend.map((point) => point.label),
      axisLine: { lineStyle: { color: "#dfe4e6" } },
      axisTick: { show: false },
      axisLabel: { color: "#657b90", fontSize: 14 },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "#74889c", fontSize: 14, formatter: primaryValueFormatter },
      splitLine: { lineStyle: { color: "#edf0f1" } },
    },
    series: [{
      name: primaryMetricLabel.value,
      type: "line",
      smooth: true,
      symbol: "circle",
      symbolSize: 6,
      lineStyle: { width: 2.5 },
      areaStyle: { color: "rgba(22, 135, 248, 0.12)" },
      data: props.card.trend.map(trendValue),
    }],
  });
  if (breakdownChartType.value === "bar") {
    const ranking = [...breakdownRanking.value].reverse();
    breakdownChart.setOption({
      animationDuration: 500,
      color: ["#1687f8"],
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        valueFormatter: primaryValueFormatter,
      },
      legend: { show: false },
      grid: { left: 10, right: 18, top: 16, bottom: 8, containLabel: true },
      xAxis: {
        type: "value",
        axisLabel: { color: "#74889c", fontSize: 14, formatter: primaryValueFormatter },
        splitLine: { lineStyle: { color: "#edf0f1" } },
      },
      yAxis: {
        type: "category",
        data: ranking.map((item) => item.label),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: "#4f5f69",
          fontSize: 14,
          width: 156,
          overflow: "truncate",
          ellipsis: "…",
        },
      },
      series: [{
        name: primaryMetricLabel.value,
        type: "bar",
        barMaxWidth: 15,
        itemStyle: { borderRadius: [0, 3, 3, 0] },
        data: ranking.map((item) => item.value),
      }],
    }, true);
  } else {
    breakdownChart.setOption({
      animationDuration: 500,
      color: ["#1687f8", "#08b4d8", "#40c7bd", "#a8bed1", "#f2a93b"],
      tooltip: {
        trigger: "item",
        formatter: (parameters: { name: string; percent: number; value: number }) => (
          `${escapeHtml(parameters.name)}<br/>${parameters.percent}% · ${primaryValueFormatter(parameters.value)}`
        ),
      },
      legend: {
        type: "scroll",
        bottom: 0,
        icon: "circle",
        itemWidth: 8,
        itemHeight: 8,
        textStyle: { color: "#596b7e", fontSize: 13 },
      },
      xAxis: { show: false },
      yAxis: { show: false },
      series: [{
        name: props.card.breakdown_title,
        type: "pie",
        radius: ["48%", "70%"],
        center: ["50%", "42%"],
        label: { show: false },
        itemStyle: { borderColor: "#fff", borderWidth: 3 },
        data: props.card.breakdown.map((item) => ({ name: item.label, value: item.value })),
      }],
    }, true);
  }
}

onMounted(async () => {
  await nextTick();
  renderCharts();
  resizeObserver = new ResizeObserver(() => {
    trendChart?.resize();
    breakdownChart?.resize();
  });
  if (trendElement.value) resizeObserver.observe(trendElement.value);
  if (breakdownElement.value) resizeObserver.observe(breakdownElement.value);
});
watch(() => props.card, () => nextTick(renderCharts), { deep: true });
onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  trendChart?.dispose();
  breakdownChart?.dispose();
});
</script>

<template>
  <section class="analytics-panel">
    <div class="analysis-heading">
      <div class="assistant-mark">Q</div>
      <div>
        <div class="analysis-title-line">
          <h3>{{ card.title }}</h3>
        </div>
        <p>{{ card.summary }}</p>
      </div>
    </div>

    <div class="metric-grid">
      <article v-for="metric in card.metrics" :key="metric.key" class="metric-card">
        <div class="metric-label">
          <span>{{ metric.label }}</span>
          <em :class="metric.trend">
            <TrendingUp v-if="metric.trend === 'up'" :size="13" />
            <TrendingDown v-else-if="metric.trend === 'down'" :size="13" />
            {{ changeLabel(metric) }}
          </em>
        </div>
        <strong>{{ metricValue(metric) }}</strong>
        <small>对比 {{ card.comparison_label }}</small>
      </article>
    </div>

    <div class="chart-grid">
      <section class="chart-panel">
        <div class="panel-title">
          <div><h4>{{ primaryMetricLabel }}趋势</h4><p>{{ chartSubtitle }}</p></div>
          <span>{{ primaryMetric?.unit || "数值" }}</span>
        </div>
        <div ref="trendElement" class="chart-canvas" role="img" :aria-label="`${primaryMetricLabel}趋势图`"></div>
      </section>
      <section class="chart-panel">
        <div class="panel-title">
          <div><h4>{{ card.breakdown_title }}</h4><p>{{ breakdownSubtitle }}</p></div>
        </div>
        <div ref="breakdownElement" class="chart-canvas" role="img" :aria-label="`${card.breakdown_title}图`"></div>
      </section>
    </div>

    <div class="analysis-notes">
      <section>
        <h4><CheckCircle2 :size="16" />分析洞察</h4>
        <p v-for="insight in card.insights" :key="insight">{{ insight }}</p>
      </section>
      <section>
        <h4><TrendingUp :size="16" />建议动作</h4>
        <ol><li v-for="item in card.recommendations" :key="item">{{ item }}</li></ol>
      </section>
      <section v-if="card.cautions.length" class="analysis-warning">
        <h4><AlertCircle :size="16" />口径说明</h4>
        <p v-for="item in card.cautions" :key="item">{{ item }}</p>
      </section>
    </div>

    <footer class="analysis-footnote">
      <CalendarRange :size="14" />{{ card.comparison_basis }} · 数据截至 {{ card.data_as_of }}
    </footer>
  </section>
</template>
