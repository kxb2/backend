from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


# 여기선 snake_case로 쓰고 JSON은 camelCase로 나가도록
class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)