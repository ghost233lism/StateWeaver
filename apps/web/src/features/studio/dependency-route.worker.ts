import { computeDependencyPlan, type RouteRequest } from './dependencyRoutePlan'
self.onmessage = (event: MessageEvent<RouteRequest>) => self.postMessage(computeDependencyPlan(event.data))
