"""Sistema de Métricas e Logging para IntentAnalyzer

Este módulo implementa um sistema abrangente de métricas, logging e monitoramento
para o sistema de detecção de intenção, seguindo melhores práticas para agentes autônomos:

- Métricas detalhadas de performance
- Logging estruturado para auditoria
- Alertas automáticos para anomalias
- Dashboards de monitoramento
- Análise de tendências
- Auto-ajuste de parâmetros
"""

import logging
import time
import json
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict, deque
from enum import Enum
import statistics

from .intent_analyzer import IntentAnalysisResult, IntentType, IntentCategory


logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    """Níveis de alerta para monitoramento"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class PerformanceMetric:
    """Métrica individual de performance"""
    name: str
    value: float
    unit: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IntentAnalysisEvent:
    """Evento de análise de intenção para logging estruturado"""
    timestamp: float
    user_input: str
    intent_result: IntentAnalysisResult
    session_context: Optional[Dict] = None
    performance_metrics: List[PerformanceMetric] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Converte para dicionário para serialização"""
        return {
            'timestamp': self.timestamp,
            'datetime': datetime.fromtimestamp(self.timestamp).isoformat(),
            'user_input_length': len(self.user_input),
            'user_input_hash': hash(self.user_input),  # Para privacy
            'intent_type': self.intent_result.intent_type.value,
            'primary_category': self.intent_result.primary_category.value,
            'confidence': self.intent_result.confidence,
            'complexity_score': self.intent_result.complexity_score,
            'detected_patterns': self.intent_result.detected_patterns,
            'matched_keywords': self.intent_result.matched_keywords,
            'analysis_time': self.intent_result.analysis_time,
            'session_context': self.session_context,
            'performance_metrics': [asdict(m) for m in self.performance_metrics],
            'anomalies': self.anomalies
        }


@dataclass
class IntentTrend:
    """Tendência de análise de intenção ao longo do tempo"""
    period: str  # "hour", "day", "week"
    intent_type: IntentType
    count: int
    avg_confidence: float
    avg_complexity: float
    trend_direction: str  # "up", "down", "stable"
    change_percentage: float


class IntentMetricsCollector:
    """Coletor avançado de métricas para IntentAnalyzer"""

    def __init__(self,
                 max_events: int = 10000,
                 metrics_retention_days: int = 30,
                 enable_file_logging: bool = True,
                 log_file_path: Optional[Path] = None):

        self.max_events = max_events
        self.metrics_retention_days = metrics_retention_days
        self.enable_file_logging = enable_file_logging

        # Configuração de logging
        if log_file_path:
            self.log_file_path = log_file_path
        else:
            self.log_file_path = Path("logs") / "intent_analysis.jsonl"

        self._setup_logging()

        # Armazenamento de eventos e métricas
        self.events: deque = deque(maxlen=max_events)
        self.performance_history: deque = deque(maxlen=max_events)

        # Métricas em tempo real
        self.real_time_metrics = {
            'total_analyses': 0,
            'successful_analyses': 0,
            'failed_analyses': 0,
            'avg_analysis_time': 0.0,
            'avg_confidence': 0.0,
            'avg_complexity': 0.0,
            'cache_hit_rate': 0.0,
            'intent_distribution': defaultdict(int),
            'category_distribution': defaultdict(int),
            'pattern_effectiveness': defaultdict(list),
            'keyword_frequency': defaultdict(int),
            'anomaly_count': 0
        }

        # Configuração de alertas
        self.alert_thresholds = {
            'max_analysis_time': 0.1,  # 100ms
            'min_confidence_rate': 0.6,  # 60% das análises devem ter confiança > 0.5
            'max_failure_rate': 0.05,  # 5% máximo de falhas
            'min_cache_hit_rate': 0.3  # 30% mínimo de cache hits
        }

        # Cache de tendências
        self._trend_cache: Dict[str, List[IntentTrend]] = {}
        self._last_trend_calculation = 0

        logger.info("IntentMetricsCollector initialized")

    def _setup_logging(self) -> None:
        """Configura sistema de logging estruturado"""
        if self.enable_file_logging:
            # Cria diretório se não existir
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)

            # Configura formatter estruturado
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )

            # Handler para arquivo
            file_handler = logging.FileHandler(self.log_file_path)
            file_handler.setFormatter(formatter)

            # Logger específico para métricas
            self.metrics_logger = logging.getLogger('deile.intent_metrics')
            self.metrics_logger.addHandler(file_handler)
            self.metrics_logger.setLevel(logging.INFO)

    def record_analysis_event(self,
                            user_input: str,
                            intent_result: IntentAnalysisResult,
                            session_context: Optional[Dict] = None,
                            anomalies: Optional[List[str]] = None) -> None:
        """Registra evento de análise de intenção"""

        timestamp = time.time()

        # Cria métricas de performance
        performance_metrics = [
            PerformanceMetric(
                name="analysis_time",
                value=intent_result.analysis_time,
                unit="seconds"
            ),
            PerformanceMetric(
                name="confidence_score",
                value=intent_result.confidence,
                unit="percentage"
            ),
            PerformanceMetric(
                name="complexity_score",
                value=intent_result.complexity_score,
                unit="percentage"
            )
        ]

        # Cria evento
        event = IntentAnalysisEvent(
            timestamp=timestamp,
            user_input=user_input,
            intent_result=intent_result,
            session_context=session_context,
            performance_metrics=performance_metrics,
            anomalies=anomalies or []
        )

        # Armazena evento
        self.events.append(event)
        self.performance_history.append(performance_metrics)

        # Atualiza métricas em tempo real
        self._update_real_time_metrics(event)

        # Verifica alertas
        self._check_alerts(event)

        # Log estruturado
        if self.enable_file_logging:
            self.metrics_logger.info(
                f"Intent analysis recorded: {json.dumps(event.to_dict())}"
            )

        logger.debug(f"Recorded analysis event: {intent_result}")

    def _update_real_time_metrics(self, event: IntentAnalysisEvent) -> None:
        """Atualiza métricas em tempo real"""

        self.real_time_metrics['total_analyses'] += 1

        # Determina se foi sucesso
        if event.intent_result.intent_type != IntentType.UNKNOWN:
            self.real_time_metrics['successful_analyses'] += 1
        else:
            self.real_time_metrics['failed_analyses'] += 1

        # Atualiza médias (média móvel simples)
        total = self.real_time_metrics['total_analyses']

        # Tempo de análise
        current_avg_time = self.real_time_metrics['avg_analysis_time']
        new_time = event.intent_result.analysis_time
        self.real_time_metrics['avg_analysis_time'] = (
            (current_avg_time * (total - 1) + new_time) / total
        )

        # Confiança
        current_avg_conf = self.real_time_metrics['avg_confidence']
        new_conf = event.intent_result.confidence
        self.real_time_metrics['avg_confidence'] = (
            (current_avg_conf * (total - 1) + new_conf) / total
        )

        # Complexidade
        current_avg_comp = self.real_time_metrics['avg_complexity']
        new_comp = event.intent_result.complexity_score
        self.real_time_metrics['avg_complexity'] = (
            (current_avg_comp * (total - 1) + new_comp) / total
        )

        # Distribuições
        self.real_time_metrics['intent_distribution'][event.intent_result.intent_type.value] += 1
        self.real_time_metrics['category_distribution'][event.intent_result.primary_category.value] += 1

        # Efetividade de padrões
        for pattern in event.intent_result.detected_patterns:
            self.real_time_metrics['pattern_effectiveness'][pattern].append(event.intent_result.confidence)

        # Frequência de keywords
        for keyword in event.intent_result.matched_keywords:
            self.real_time_metrics['keyword_frequency'][keyword] += 1

        # Anomalias
        if event.anomalies:
            self.real_time_metrics['anomaly_count'] += len(event.anomalies)

    def _check_alerts(self, event: IntentAnalysisEvent) -> None:
        """Verifica condições de alerta"""

        alerts = []

        # Tempo de análise muito alto
        if event.intent_result.analysis_time > self.alert_thresholds['max_analysis_time']:
            alerts.append({
                'level': AlertLevel.WARNING,
                'message': f"High analysis time: {event.intent_result.analysis_time:.3f}s",
                'threshold': self.alert_thresholds['max_analysis_time']
            })

        # Confiança muito baixa frequentemente
        recent_events = list(self.events)[-100:]  # últimos 100 eventos
        if len(recent_events) >= 50:
            low_confidence_rate = sum(
                1 for e in recent_events
                if e.intent_result.confidence < 0.5
            ) / len(recent_events)

            if low_confidence_rate > (1 - self.alert_thresholds['min_confidence_rate']):
                alerts.append({
                    'level': AlertLevel.WARNING,
                    'message': f"High low-confidence rate: {low_confidence_rate:.2%}",
                    'threshold': self.alert_thresholds['min_confidence_rate']
                })

        # Taxa de falha muito alta
        total = self.real_time_metrics['total_analyses']
        if total >= 20:
            failure_rate = self.real_time_metrics['failed_analyses'] / total
            if failure_rate > self.alert_thresholds['max_failure_rate']:
                alerts.append({
                    'level': AlertLevel.ERROR,
                    'message': f"High failure rate: {failure_rate:.2%}",
                    'threshold': self.alert_thresholds['max_failure_rate']
                })

        # Anomalias detectadas
        if event.anomalies:
            alerts.append({
                'level': AlertLevel.INFO,
                'message': f"Anomalies detected: {', '.join(event.anomalies)}",
                'anomalies': event.anomalies
            })

        # Log alertas
        for alert in alerts:
            if alert['level'] == AlertLevel.CRITICAL:
                logger.critical(f"Intent Analysis Alert: {alert['message']}")
            elif alert['level'] == AlertLevel.ERROR:
                logger.error(f"Intent Analysis Alert: {alert['message']}")
            elif alert['level'] == AlertLevel.WARNING:
                logger.warning(f"Intent Analysis Alert: {alert['message']}")
            else:
                logger.info(f"Intent Analysis Alert: {alert['message']}")

    def get_comprehensive_metrics(self) -> Dict[str, Any]:
        """Retorna métricas abrangentes do sistema"""

        current_time = time.time()

        # Métricas básicas
        basic_metrics = dict(self.real_time_metrics)

        # Calcula success rate
        total = basic_metrics['total_analyses']
        if total > 0:
            basic_metrics['success_rate'] = basic_metrics['successful_analyses'] / total
            basic_metrics['failure_rate'] = basic_metrics['failed_analyses'] / total
        else:
            basic_metrics['success_rate'] = 0.0
            basic_metrics['failure_rate'] = 0.0

        # Estatísticas avançadas dos últimos eventos
        recent_events = list(self.events)[-1000:]  # últimos 1000 eventos

        advanced_metrics = {}
        if recent_events:
            analysis_times = [e.intent_result.analysis_time for e in recent_events]
            confidences = [e.intent_result.confidence for e in recent_events]
            complexities = [e.intent_result.complexity_score for e in recent_events]

            advanced_metrics = {
                'analysis_time_stats': {
                    'min': min(analysis_times),
                    'max': max(analysis_times),
                    'median': statistics.median(analysis_times),
                    'std_dev': statistics.stdev(analysis_times) if len(analysis_times) > 1 else 0
                },
                'confidence_stats': {
                    'min': min(confidences),
                    'max': max(confidences),
                    'median': statistics.median(confidences),
                    'std_dev': statistics.stdev(confidences) if len(confidences) > 1 else 0
                },
                'complexity_stats': {
                    'min': min(complexities),
                    'max': max(complexities),
                    'median': statistics.median(complexities),
                    'std_dev': statistics.stdev(complexities) if len(complexities) > 1 else 0
                }
            }

        # Efetividade de padrões (média de confiança por padrão)
        pattern_effectiveness = {}
        for pattern, confidence_scores in self.real_time_metrics['pattern_effectiveness'].items():
            if confidence_scores:
                pattern_effectiveness[pattern] = {
                    'avg_confidence': statistics.mean(confidence_scores),
                    'usage_count': len(confidence_scores),
                    'effectiveness_score': statistics.mean(confidence_scores) * len(confidence_scores)
                }

        # Tendências
        trends = self.calculate_trends()

        return {
            'timestamp': current_time,
            'basic_metrics': basic_metrics,
            'advanced_metrics': advanced_metrics,
            'pattern_effectiveness': pattern_effectiveness,
            'trends': trends,
            'system_health': self._assess_system_health(),
            'recommendations': self._generate_recommendations()
        }

    def calculate_trends(self, force_refresh: bool = False) -> Dict[str, List[IntentTrend]]:
        """Calcula tendências de análise"""

        current_time = time.time()

        # Cache de 5 minutos
        if not force_refresh and (current_time - self._last_trend_calculation) < 300:
            return self._trend_cache

        trends = {
            'hourly': [],
            'daily': [],
            'weekly': []
        }

        if len(self.events) < 10:  # Dados insuficientes
            return trends

        recent_events = list(self.events)

        # Calcula tendências por período
        for period, seconds in [('hourly', 3600), ('daily', 86400), ('weekly', 604800)]:
            period_trends = self._calculate_period_trends(recent_events, seconds)
            trends[period] = period_trends

        self._trend_cache = trends
        self._last_trend_calculation = current_time

        return trends

    def _calculate_period_trends(self, events: List[IntentAnalysisEvent], period_seconds: int) -> List[IntentTrend]:
        """Calcula tendências para um período específico"""

        current_time = time.time()
        period_start = current_time - period_seconds

        # Filtra eventos do período
        period_events = [e for e in events if e.timestamp >= period_start]

        if len(period_events) < 5:  # Dados insuficientes
            return []

        # Agrupa por tipo de intenção
        intent_groups = defaultdict(list)
        for event in period_events:
            intent_groups[event.intent_result.intent_type].append(event)

        trends = []

        for intent_type, intent_events in intent_groups.items():
            if len(intent_events) < 2:
                continue

            # Calcula métricas
            confidences = [e.intent_result.confidence for e in intent_events]
            complexities = [e.intent_result.complexity_score for e in intent_events]

            avg_confidence = statistics.mean(confidences)
            avg_complexity = statistics.mean(complexities)

            # Calcula tendência (simplificada)
            recent_half = intent_events[len(intent_events)//2:]
            older_half = intent_events[:len(intent_events)//2]

            if recent_half and older_half:
                recent_avg = statistics.mean([e.intent_result.confidence for e in recent_half])
                older_avg = statistics.mean([e.intent_result.confidence for e in older_half])

                change_percentage = ((recent_avg - older_avg) / older_avg) * 100 if older_avg > 0 else 0

                if change_percentage > 5:
                    trend_direction = "up"
                elif change_percentage < -5:
                    trend_direction = "down"
                else:
                    trend_direction = "stable"
            else:
                change_percentage = 0
                trend_direction = "stable"

            trend = IntentTrend(
                period=f"{period_seconds//3600}h" if period_seconds < 86400 else f"{period_seconds//86400}d",
                intent_type=intent_type,
                count=len(intent_events),
                avg_confidence=avg_confidence,
                avg_complexity=avg_complexity,
                trend_direction=trend_direction,
                change_percentage=change_percentage
            )

            trends.append(trend)

        return trends

    def _assess_system_health(self) -> Dict[str, Any]:
        """Avalia saúde geral do sistema"""

        health_score = 100.0
        issues = []

        # Verifica métricas críticas
        if self.real_time_metrics['total_analyses'] > 0:
            failure_rate = self.real_time_metrics['failed_analyses'] / self.real_time_metrics['total_analyses']

            if failure_rate > 0.1:
                health_score -= 30
                issues.append(f"High failure rate: {failure_rate:.2%}")
            elif failure_rate > 0.05:
                health_score -= 15
                issues.append(f"Elevated failure rate: {failure_rate:.2%}")

        # Verifica tempo de análise
        avg_time = self.real_time_metrics['avg_analysis_time']
        if avg_time > 0.1:
            health_score -= 20
            issues.append(f"Slow analysis time: {avg_time:.3f}s")
        elif avg_time > 0.05:
            health_score -= 10
            issues.append(f"Elevated analysis time: {avg_time:.3f}s")

        # Verifica confiança média
        avg_confidence = self.real_time_metrics['avg_confidence']
        if avg_confidence < 0.4:
            health_score -= 25
            issues.append(f"Low average confidence: {avg_confidence:.2f}")
        elif avg_confidence < 0.6:
            health_score -= 10
            issues.append(f"Below-optimal confidence: {avg_confidence:.2f}")

        # Determina status
        if health_score >= 90:
            status = "excellent"
        elif health_score >= 80:
            status = "good"
        elif health_score >= 70:
            status = "fair"
        elif health_score >= 60:
            status = "poor"
        else:
            status = "critical"

        return {
            'status': status,
            'health_score': max(0, health_score),
            'issues': issues,
            'last_assessment': time.time()
        }

    def _generate_recommendations(self) -> List[Dict[str, Any]]:
        """Gera recomendações para otimização"""

        recommendations = []

        # Recomendações baseadas em métricas
        if self.real_time_metrics['avg_analysis_time'] > 0.05:
            recommendations.append({
                'type': 'performance',
                'priority': 'high',
                'message': 'Consider optimizing regex patterns or increasing cache size',
                'metric': 'analysis_time'
            })

        if self.real_time_metrics['avg_confidence'] < 0.6:
            recommendations.append({
                'type': 'accuracy',
                'priority': 'medium',
                'message': 'Review and enhance intent patterns configuration',
                'metric': 'confidence'
            })

        # Recomendações baseadas em padrões
        pattern_effectiveness = self.real_time_metrics['pattern_effectiveness']

        # Identifica padrões pouco efetivos
        ineffective_patterns = [
            pattern for pattern, scores in pattern_effectiveness.items()
            if scores and statistics.mean(scores) < 0.4
        ]

        if ineffective_patterns:
            recommendations.append({
                'type': 'pattern_optimization',
                'priority': 'medium',
                'message': f'Consider reviewing patterns: {", ".join(ineffective_patterns[:3])}',
                'patterns': ineffective_patterns
            })

        # Recomendações baseadas em cache
        if len(self.events) > 100:
            unique_inputs = len(set(hash(e.user_input) for e in list(self.events)[-100:]))
            if unique_inputs / 100 > 0.8:  # Mais de 80% de inputs únicos
                recommendations.append({
                    'type': 'cache_optimization',
                    'priority': 'low',
                    'message': 'High input diversity detected, consider increasing cache size',
                    'metric': 'cache_diversity'
                })

        return recommendations

    def export_metrics(self, format: str = "json", file_path: Optional[Path] = None) -> Optional[str]:
        """Exporta métricas para arquivo"""

        metrics = self.get_comprehensive_metrics()

        if format.lower() == "json":
            content = json.dumps(metrics, indent=2, default=str)
            extension = ".json"
        else:
            raise ValueError(f"Unsupported format: {format}")

        if file_path:
            output_path = file_path
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = Path(f"intent_metrics_{timestamp}{extension}")

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"Metrics exported to {output_path}")
            return str(output_path)
        except Exception as e:
            logger.error(f"Failed to export metrics: {e}")
            return None

    def clear_old_data(self) -> None:
        """Remove dados antigos baseado na configuração de retenção"""

        cutoff_time = time.time() - (self.metrics_retention_days * 86400)

        # Remove eventos antigos
        old_count = len(self.events)
        self.events = deque(
            (e for e in self.events if e.timestamp >= cutoff_time),
            maxlen=self.max_events
        )
        new_count = len(self.events)

        if old_count != new_count:
            logger.info(f"Cleaned {old_count - new_count} old events from metrics storage")

        # Limpa cache de tendências
        self._trend_cache.clear()
        self._last_trend_calculation = 0


# Instância singleton para uso global
_metrics_collector: Optional[IntentMetricsCollector] = None


def get_intent_metrics_collector(**kwargs) -> IntentMetricsCollector:
    """Retorna instância singleton do coletor de métricas"""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = IntentMetricsCollector(**kwargs)
    return _metrics_collector