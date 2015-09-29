from cloudify import ctx

ctx.logger.info('Injecting old agent data')
agent = ctx.source.instance.runtime_properties['cloudify_agent']
agent['version'] = '3.3'
ctx.target.instance.runtime_properties['cloudify_old_agents'] = [agent]
