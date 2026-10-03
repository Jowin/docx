from .base import AgentError, ConfirmationRequired, CorpusTooSmall, DesignAgent
from .detection_rules import DetectionRulesAgent, DetectionRulesInput, DetectionRulesOutput
from .extraction_judge import ExtractionJudge, ExtractionJudgeInput, ExtractionJudgeOutput
from .evaluation import EvaluationAgent, EvaluationInput, EvaluationOutput
from .field_schema import FieldSchemaAgent, FieldSchemaInput, FieldSchemaOutput
from .pattern_skill_writer import (
    PatternSkillWriter,
    PatternSkillWriterInput,
    PatternSkillWriterOutput,
)
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
    ExtractionJudge.name: ExtractionJudge,
    PatternSkillWriter.name: PatternSkillWriter,
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
    "ExtractionJudge",
    "ExtractionJudgeInput",
    "ExtractionJudgeOutput",
    "FieldSchemaAgent",
    "FieldSchemaInput",
    "FieldSchemaOutput",
    "Packager",
    "PackagerInput",
    "PackagerOutput",
    "PatternSkillWriter",
    "PatternSkillWriterInput",
    "PatternSkillWriterOutput",
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
