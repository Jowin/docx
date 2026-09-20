from .base import AgentError, ConfirmationRequired, CorpusTooSmall, DesignAgent
from .detection_rules import DetectionRulesAgent, DetectionRulesInput, DetectionRulesOutput
from .evaluation import EvaluationAgent, EvaluationInput, EvaluationOutput
from .field_schema import FieldSchemaAgent, FieldSchemaInput, FieldSchemaOutput
from .packager import Packager, PackagerInput, PackagerOutput, SkillBundle
from .profiler import CorpusProfiler, ProfilerInput, ProfilerOutput
from .skill_author import SkillAuthorAgent, SkillAuthorInput, SkillAuthorOutput
from .threshold_tuner import (
    Prediction,
    ThresholdTuner,
    ThresholdTunerInput,
    ThresholdTunerOutput,
)
from .type_discovery import TypeDiscovery, TypeDiscoveryInput, TypeDiscoveryOutput

#: name -> agent class, for the API layer and for run orchestration.
AGENTS = {
    CorpusProfiler.name: CorpusProfiler,
    TypeDiscovery.name: TypeDiscovery,
    FieldSchemaAgent.name: FieldSchemaAgent,
    DetectionRulesAgent.name: DetectionRulesAgent,
    SkillAuthorAgent.name: SkillAuthorAgent,
    ThresholdTuner.name: ThresholdTuner,
    EvaluationAgent.name: EvaluationAgent,
    Packager.name: Packager,
}

__all__ = [
    "AGENTS",
    "AgentError",
    "ConfirmationRequired",
    "CorpusProfiler",
    "CorpusTooSmall",
    "DesignAgent",
    "DetectionRulesAgent",
    "DetectionRulesInput",
    "DetectionRulesOutput",
    "EvaluationAgent",
    "EvaluationInput",
    "EvaluationOutput",
    "FieldSchemaAgent",
    "FieldSchemaInput",
    "FieldSchemaOutput",
    "Packager",
    "PackagerInput",
    "PackagerOutput",
    "Prediction",
    "ProfilerInput",
    "ProfilerOutput",
    "SkillAuthorAgent",
    "SkillAuthorInput",
    "SkillAuthorOutput",
    "SkillBundle",
    "ThresholdTuner",
    "ThresholdTunerInput",
    "ThresholdTunerOutput",
    "TypeDiscovery",
    "TypeDiscoveryInput",
    "TypeDiscoveryOutput",
]
