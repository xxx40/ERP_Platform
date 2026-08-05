<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { PresentationBlock } from "./types";

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const props = defineProps<{ block: PresentationBlock }>();
const element = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;
let observer: ResizeObserver | null = null;

function render() {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  const type = props.block.chart_type ?? "bar";
  const series = props.block.series.map((item) => ({
    name: item.name ?? "数值",
    type,
    data: type === "pie"
      ? (item.data ?? []).map((value, index) => ({ value, name: props.block.x_axis[index] }))
      : item.data ?? [],
    smooth: type === "line",
  }));
  chart.setOption({
    color: ["#1687f8", "#17a673", "#f2a93b", "#748aa1"],
    tooltip: { trigger: type === "pie" ? "item" : "axis" },
    legend: { bottom: 0 },
    grid: { left: 48, right: 20, top: 18, bottom: 48 },
    xAxis: type === "pie" ? undefined : { type: "category", data: props.block.x_axis },
    yAxis: type === "pie" ? undefined : { type: "value" },
    series,
  }, true);
}

onMounted(() => {
  render();
  observer = new ResizeObserver(() => chart?.resize());
  if (element.value) observer.observe(element.value);
});
watch(() => props.block, render, { deep: true });
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose(); });
</script>

<template><div ref="element" class="generic-chart" role="img" :aria-label="block.title ?? '数据图表'"></div></template>
